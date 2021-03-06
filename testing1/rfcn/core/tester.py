# --------------------------------------------------------
# Deformable Convolutional Networks
# Copyright (c) 2016 by Contributors
# Copyright (c) 2017 Microsoft
# Licensed under The Apache-2.0 License [see LICENSE for details]
# Modified by Yuwen Xiong
# --------------------------------------------------------

import cPickle
import os
import time
import mxnet as mx
import numpy as np
import json
import sys

from module import MutableModule
from utils import image
from bbox.bbox_transform import bbox_pred, clip_boxes
from nms.nms import py_nms_wrapper, py_softnms_wrapper, cpu_nms_wrapper, gpu_nms_wrapper
from utils.PrefetchingIter import PrefetchingIter
from multiprocessing import Pool

class Predictor(object):
    def __init__(self, symbol, data_names, label_names,
                 context=mx.cpu(), max_data_shapes=None,
                 provide_data=None, provide_label=None,
                 arg_params=None, aux_params=None):
        self._mod = MutableModule(symbol, data_names, label_names,
                                  context=context, max_data_shapes=max_data_shapes)
        self._mod.bind(provide_data, provide_label, for_training=False)
        self._mod.init_params(arg_params=arg_params, aux_params=aux_params)

    def predict(self, data_batch):
        self._mod.forward(data_batch)
        # [dict(zip(self._mod.output_names, _)) for _ in zip(*self._mod.get_outputs(merge_multi_context=False))]
        return [dict(zip(self._mod.output_names, _)) for _ in zip(*self._mod.get_outputs(merge_multi_context=False))]


def im_proposal(predictor, data_batch, data_names, scales):
    output_all = predictor.predict(data_batch)

    data_dict_all = [dict(zip(data_names, data_batch.data[i])) for i in xrange(len(data_batch.data))]
    scores_all = []
    boxes_all = []

    for output, data_dict, scale in zip(output_all, data_dict_all, scales):
        # drop the batch index
        boxes = output['rois_output'].asnumpy()[:, 1:]
        scores = output['rois_score'].asnumpy()

        # transform to original scale
        boxes = boxes / scale
        scores_all.append(scores)
        boxes_all.append(boxes)

    return scores_all, boxes_all, data_dict_all


def generate_proposals(predictor, test_data, imdb, cfg, vis=False, thresh=0.):
    """
    Generate detections results using RPN.
    :param predictor: Predictor
    :param test_data: data iterator, must be non-shuffled
    :param imdb: image database
    :param vis: controls visualization
    :param thresh: thresh for valid detections
    :return: list of detected boxes
    """
    assert vis or not test_data.shuffle
    data_names = [k[0] for k in test_data.provide_data[0]]

    if not isinstance(test_data, PrefetchingIter):
        test_data = PrefetchingIter(test_data)

    idx = 0
    t = time.time()
    imdb_boxes = list()
    original_boxes = list()
    for im_info, data_batch in test_data:
        t1 = time.time() - t
        t = time.time()

        scales = [iim_info[0, 2] for iim_info in im_info]
        scores_all, boxes_all, data_dict_all = im_proposal(predictor, data_batch, data_names, scales)
        t2 = time.time() - t
        t = time.time()
        for delta, (scores, boxes, data_dict, scale) in enumerate(zip(scores_all, boxes_all, data_dict_all, scales)):
            # assemble proposals
            dets = np.hstack((boxes, scores))
            original_boxes.append(dets)

            # filter proposals
            keep = np.where(dets[:, 4:] > thresh)[0]
            dets = dets[keep, :]
            imdb_boxes.append(dets)

            if vis:
                vis_all_detection(data_dict['data'].asnumpy(), [dets], ['obj'], scale, cfg)

            print 'generating %d/%d' % (idx + 1, imdb.num_images), 'proposal %d' % (dets.shape[0]), \
                'data %.4fs net %.4fs' % (t1, t2 / test_data.batch_size)
            idx += 1


    assert len(imdb_boxes) == imdb.num_images, 'calculations not complete'

    # save results
    rpn_folder = os.path.join(imdb.result_path, 'rpn_data')
    if not os.path.exists(rpn_folder):
        os.mkdir(rpn_folder)

    rpn_file = os.path.join(rpn_folder, imdb.name + '_rpn.pkl')
    with open(rpn_file, 'wb') as f:
        cPickle.dump(imdb_boxes, f, cPickle.HIGHEST_PROTOCOL)

    if thresh > 0:
        full_rpn_file = os.path.join(rpn_folder, imdb.name + '_full_rpn.pkl')
        with open(full_rpn_file, 'wb') as f:
            cPickle.dump(original_boxes, f, cPickle.HIGHEST_PROTOCOL)

    print 'wrote rpn proposals to {}'.format(rpn_file)
    return imdb_boxes


def im_detect(predictor, data_batch, data_names, scales, cfg):
    output_all = predictor.predict(data_batch)

    data_dict_all = [dict(zip(data_names, idata)) for idata in data_batch.data]
    scores_all = []
    pred_boxes_all = []
    for output, data_dict, scale in zip(output_all, data_dict_all, scales):
        if cfg.TEST.HAS_RPN:
            rois = output['rois_output'].asnumpy()[:, 1:]
        else:
            rois = data_dict['rois'].asnumpy().reshape((-1, 5))[:, 1:]
        im_shape = data_dict['data'].shape

        # save output
        scores = output['cls_prob_reshape_output'].asnumpy()[0]
        bbox_deltas = output['bbox_pred_reshape_output'].asnumpy()[0]

        # post processing
        pred_boxes = bbox_pred(rois, bbox_deltas)
        pred_boxes = clip_boxes(pred_boxes, im_shape[-2:])

        # we used scaled image & roi to train, so it is necessary to transform them back
        pred_boxes = pred_boxes / scale

        scores_all.append(scores)
        pred_boxes_all.append(pred_boxes)
    return scores_all, pred_boxes_all, data_dict_all

# def psoft(cls_dets):
#     cls_dets = soft_nms(cls_dets, method=2)
#     return cls_dets

def pred_eval(predictor, test_data, imdb, cfg, vis=False, thresh=1e-3, logger=None, ignore_cache=True):
#def pred_eval(predictor, test_data, imdb, cfg, vis=False, thresh=0.7, logger=None, ignore_cache=True):
    """
    wrapper for calculating offline validation for faster data analysis
    in this example, all threshold are set by hand
    :param predictor: Predictor
    :param test_data: data iterator, must be non-shuffle
    :param imdb: image database
    :param vis: controls visualization
    :param thresh: valid detection threshold
    :return:
    """
    co_occur_matrix = np.load('/home/user/Deformable-ConvNets2/tmp/co_occur_matrix.npy')
    nor_co_occur_matrix = np.zeros((90,90))
    row_max = np.zeros(90)
    co_occur_matrix = co_occur_matrix.astype(int)
    for ind, val in enumerate(co_occur_matrix):        
        row_sum = np.sum(co_occur_matrix[:,ind])        
        if not row_sum == 0:
            nor_co_occur_matrix[:,ind] = co_occur_matrix[:,ind]/row_sum
        row_max[ind] = np.amax(nor_co_occur_matrix[:,ind])
        

    assert vis or not test_data.shuffle
    data_names = [k[0] for k in test_data.provide_data[0]]    

    roidb = test_data.roidb

    if not isinstance(test_data, PrefetchingIter):
        test_data = PrefetchingIter(test_data)    
    
    soft_nms = py_softnms_wrapper(cfg.TEST.NMS)

    # limit detections to max_per_image over all classes
    max_per_image = cfg.TEST.max_per_image

    num_images = imdb.num_images

    # all detections are collected into:    
    all_boxes = [[[] for _ in range(num_images)]
                 for _ in range(imdb.num_classes)]

    idx = 0
    data_time, net_time, post_time = 0.0, 0.0, 0.0
    t = time.time()
    #pl = Pool(8)

    annotation_file = '/home/user/Deformable-ConvNets-test/data/coco/annotations/kinstances_unlabeled2017.json'
    dataset = json.load(open(annotation_file, 'r'))    
    annotations = []    
    id_count = 1
    img_count = 1

    for im_info, data_batch in test_data:
        t1 = time.time() - t
        t = time.time()

        scales = [iim_info[0, 2] for iim_info in im_info]
        scores_all, boxes_all, data_dict_all = im_detect(predictor, data_batch, data_names, scales, cfg)
        
        t2 = time.time() - t
        t = time.time()
        for delta, (scores, boxes, data_dict) in enumerate(zip(scores_all, boxes_all, data_dict_all)):            
            for j in range(1, imdb.num_classes):
                indexes = np.where(scores[:, j] > thresh)[0]
                cls_scores = scores[indexes, j, np.newaxis]
                cls_boxes = boxes[indexes, 4:8] if cfg.CLASS_AGNOSTIC else boxes[indexes, j * 4:(j + 1) * 4]
                cls_dets = np.hstack((cls_boxes, cls_scores))                
                keep = soft_nms(cls_dets)
                keep = keep.tolist()                
                all_boxes[j][idx+delta] = cls_dets[keep, :]                
            
            if max_per_image > 0:
                image_scores = np.hstack([all_boxes[j][idx+delta][:, -1]
                                          for j in range(1, imdb.num_classes)])
                if len(image_scores) > max_per_image:
                    image_thresh = np.sort(image_scores)[-max_per_image]
                    for j in range(1, imdb.num_classes):
                        keep = np.where(all_boxes[j][idx+delta][:, -1] >= image_thresh)[0]
                        all_boxes[j][idx+delta] = all_boxes[j][idx+delta][keep, :]

            if vis:                
                boxes_this_image = [[]] + [all_boxes[j][idx+delta] for j in range(1, imdb.num_classes)]
                im_name = roidb[idx]['image']
                im_name = im_name.rsplit("/", 1)
                im_name = im_name[-1]                                
                result = draw_all_detection(data_dict['data'].asnumpy(), boxes_this_image, imdb.classes, 
                                            scales[delta], cfg, im_name, annotations, id_count, 
                                            nor_co_occur_matrix, row_max)
                annotations = result['ann']
                id_count = result['id_count']                
        
        idx += test_data.batch_size
        t3 = time.time() - t
        t = time.time()
        data_time += t1
        net_time += t2
        post_time += t3
        print 'testing {}/{} data {:.4f}s net {:.4f}s post {:.4f}s'.format(idx, imdb.num_images, data_time / idx * test_data.batch_size, net_time / idx * test_data.batch_size, post_time / idx * test_data.batch_size)
        if logger:
            logger.info('testing {}/{} data {:.4f}s net {:.4f}s post {:.4f}s'.format(idx, imdb.num_images, data_time / idx * test_data.batch_size, net_time / idx * test_data.batch_size, post_time / idx * test_data.batch_size))
        
    dataset.update({'annotations':annotations})
    save_annotation_file = '/home/user/Deformable-ConvNets-test/data/coco/annotations/instances_unlabeled2017_ssl.json'
    with open(save_annotation_file, 'w') as f:
        json.dump(dataset, f)

    print "Finish generate pseudo ground truth!"

def vis_all_detection(im_array, detections, class_names, scale, cfg, threshold=1e-3):
    """
    visualize all detections in one image
    :param im_array: [b=1 c h w] in rgb
    :param detections: [ numpy.ndarray([[x1 y1 x2 y2 score]]) for j in classes ]
    :param class_names: list of names in imdb
    :param scale: visualize the scaled image
    :return:
    """
    import matplotlib.pyplot as plt
    import random
    im = image.transform_inverse(im_array, cfg.network.PIXEL_MEANS)
    plt.imshow(im)
    for j, name in enumerate(class_names):
        if name == '__background__':
            continue
        color = (random.random(), random.random(), random.random())  # generate a random color
        dets = detections[j]
        for det in dets:
            bbox = det[:4] * scale
            score = det[-1]
            if score < threshold:
                continue
            rect = plt.Rectangle((bbox[0], bbox[1]),
                                 bbox[2] - bbox[0],
                                 bbox[3] - bbox[1], fill=False,
                                 edgecolor=color, linewidth=3.5)
            plt.gca().add_patch(rect)
            plt.gca().text(bbox[0], bbox[1] - 2,
                           '{:s} {:.3f}'.format(name, score),
                           bbox=dict(facecolor=color, alpha=0.5), fontsize=12, color='white')
    plt.show()


def draw_all_detection(im_array, detections, class_names, scale, cfg, im_name, annotations, id_count, 
                    nor_co_occur_matrix, row_max, threshold=0.5, co_thres = 0.3):
    """
    visualize all detections in one image
    :param im_array: [b=1 c h w] in rgb
    :param detections: [ numpy.ndarray([[x1 y1 x2 y2 score]]) for j in classes ]
    :param class_names: list of names in imdb
    :param scale: visualize the scaled image
    :return:
    """
    import cv2
    import random    

    color_white = (255, 255, 255)
    color_red = (255, 0, 0)    

    read_name = '/home/user/Deformable-ConvNets-test/data/coco/images/unlabeled2017/' + im_name
    im = cv2.imread(read_name)
    img_id = im_name.split(".",1)
    img_id = img_id[0]
    img_id = int(img_id.lstrip('0'))
    
    ind_map = {'1':1, '2':2, '3':3, '4':4, '5':5, '6':6, '7':7, '8':8, '9':9, '10':10, '11':11, '12':13, 
            '13':14, '14':15, '15':16, '16':17, '17':18, '18':19, '19':20, '20':21, '21':22, '22':23, 
            '23':24, '24':25, '25':27, '26':28, '27':31, '28':32, '29':33, '30':34, '31':35, '32':36,
            '33':37, '34':38, '35':39, '36':40, '37':41, '38':42, '39':43, '40':44, '41':46, '42':47,
            '43':48, '44':49, '45':50, '46':51, '47':52, '48':53, '49':54, '50':55, '51':56, '52':57,
            '53':58, '54':59, '55':60, '56':61, '57':62, '58':63, '59':64, '60':65, '61':67, '62':70,
            '63':72, '64':73, '65':74, '66':75, '67':76, '68':77, '69':78, '70':79, '71':80, '72':81,
            '73':82, '74':84, '75':85, '76':86, '77':87, '78':88, '79':89, '80':90}
    
    categories = []

    for j, name in enumerate(class_names):        
        dets = detections[j]
        for det in dets:            
            score = det[-1]
            if score < threshold:
                continue
            
            ann = {'category_id': ind_map[str(j)], 'score': score}            
            categories.append(ind_map[str(j)])
            
            id_count = id_count + 1           

    categories = list(set(categories))    
    
    flag = 0
    if len(categories) > 1:
        flag = 1
        max_co_list = []
        for ind1 in categories:            
            max_co = 0
            max_dic = {ind1:max_co}
            for ind2 in categories:                
                if nor_co_occur_matrix[ind1-1][ind2-1] > max_co:                    
                    max_dic.update({ind1:nor_co_occur_matrix[ind2-1][ind1-1]/row_max[ind1-1]})
                    max_co = nor_co_occur_matrix[ind1-1][ind2-1]                    
            max_co_list.append(max_dic)

    for j, name in enumerate(class_names):
        if name == '__background__':
            continue
        color = (random.randint(0, 256), random.randint(0, 256), random.randint(0, 256))  # generate a random color
        dets = detections[j]
        for det in dets:            
            bbox = det[:4]
            score = det[-1]
            if score < threshold:
                continue            
            
            ind_j = ind_map[str(j)]
            co_occur = 0
            if score < 0.9 and flag == 1:
                for x in max_co_list:
                    if x.keys()[0] == ind_j:
                        co_occur = x[ind_j]                
                score = score*co_occur               
                
                if score < co_thres:
                    continue

            # add annotations for bbox
            area = float((bbox[2]-bbox[0])*(bbox[3]-bbox[1]))
            json_bbox = [float(bbox[0]), float(bbox[1]), float(bbox[2]-bbox[0]), float(bbox[3]-bbox[1])]            
            ann = {'segmentation':[[0,0,0,0]],'area': area,'iscrowd': 0,'image_id': img_id, 'bbox': json_bbox, 
                'category_id': ind_map[str(j)], 'id': id_count}

            annotations.append(ann)
            id_count = id_count + 1

            # draw bbox
            bbox = map(int, bbox)
            cv2.rectangle(im, (bbox[0], bbox[1]), (bbox[2], bbox[3]), color=color, thickness=3)
            cv2.putText(im, '%s %.3f' % (class_names[j], score), (bbox[0], bbox[1] + 10),
                        color=color_white, fontFace=cv2.FONT_HERSHEY_COMPLEX, fontScale=1.0)
    
    im_name = '/home/user/Deformable-ConvNets-test/tmp/unlabeled2017_' + str(threshold) + '_co_' + str(co_thres) + '/' + im_name    
    print(im_name)    
    cv2.imwrite(im_name, im)    
    result = {'ann':annotations, 'id_count':id_count}    
    return result
