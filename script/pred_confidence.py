import numpy as np
import h5py
import caffe
import cv2

caffe.set_device(0)
caffe.set_mode_gpu()
infh = h5py.File('nusc_baidu_confidence.h5', 'r')
in_feature = infh['data'].value

net = caffe.Net('data/pred_confidence.prototxt',
                'nusc_baidu_confidence_iter_106.caffemodel',
                caffe.TEST)

in_feature = infh['data'].value
print(in_feature.shape)

net.blobs['data'].data[...] = in_feature
out = net.forward()
conf = out['confidence_score']
print(conf.shape)
conf = np.transpose(
    conf, (0, 3, 2, 1))  # NxCxHxW -> NxWxHxC
print(conf.shape)
conf = conf.reshape(640, 640)
print(conf.shape)

mean = np.mean(conf)
print(mean)
conf[np.where(conf > mean)] = 255

cv2.imwrite("confidence_pred.png", conf)