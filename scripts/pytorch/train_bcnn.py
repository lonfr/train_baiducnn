#!/usr/bin/env python3
# coding: utf-8

import argparse
import os

import gdown
import numpy as np
import torch
import torch.optim as optim
# import torch_optimizer as optim
import visdom
from datetime import datetime

from BCNN import BCNN
from BcnnLoss import BcnnLoss
from NuscData import load_dataset


def train(data_path, batch_size, max_epoch, pretrained_model,
          train_data_num, test_data_num,
          width, height, use_constant_feature, use_intensity_feature):

    train_dataloader, test_dataloader = load_dataset(data_path, batch_size)
    now = datetime.now().strftime('%Y%m%d_%H%M')
    best_loss = 1e10
    vis = visdom.Visdom()
    vis_interval = 1

    if use_constant_feature and use_intensity_feature:
        in_channels = 8
        non_empty_channle = 7
    elif use_constant_feature or use_intensity_feature:
        in_channels = 6
        non_empty_channle = 5
    else:
        in_channels = 4
        non_empty_channle = 3

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    bcnn_model = BCNN(in_channels=in_channels, n_class=6).to(device)
    bcnn_model = torch.nn.DataParallel(bcnn_model)  # multi gpu

    if os.path.exists(pretrained_model):
        print('Use pretrained model')
        bcnn_model.load_state_dict(torch.load(pretrained_model))
    else:
        print('Not found ', pretrained_model)
        if pretrained_model == 'checkpoints/bestmodel.pt':
            print('Downloading ', pretrained_model)
            gdown.cached_download(
                'https://drive.google.com/uc?export=download&id=19IPtsVes3w-qogsiJToHmLrjCAdVEl9K',
                pretrained_model,
                md5='b124dab72fd6f2b642c6e46e5b142ebf')
            bcnn_model.load_state_dict(torch.load(pretrained_model))

    bcnn_model.eval()
    save_model_interval = 1

    transfer_learning = False
    if transfer_learning:
        params_to_update = []
        update_param_names = ["deconv0.weight", "deconv0.bias"]
        for name, param in bcnn_model.named_parameters():
            if name in update_param_names:
                param.requires_grad = True
                params_to_update.append(param)
                print(name)
            else:
                param.requires_grad = False
        print("-----------")
        print(params_to_update)
        optimizer = optim.SGD(params=params_to_update, lr=1e-5, momentum=0.9)
    else:
        # optimizer = optim.RAdam(
        #     bcnn_model.parameters(),
        #     lr=2e-6,
        #     betas=(0.9, 0.999),
        #     eps=1e-8,
        #     weight_decay=0,
        # )
        # optimizer = optim.AdaBound(
        #     bcnn_model.parameters(),
        #     lr=1e-4,
        #     betas=(0.9, 0.999),
        #     final_lr=0.1,
        #     gamma=1e-3,
        #     eps=1e-8,
        #     weight_decay=0,
        #     amsbound=False,
        # )
        # optimizer = optim.Adam(bcnn_model.parameters(), lr=1e-3)
        optimizer = optim.SGD(bcnn_model.parameters(), lr=2e-6, momentum=0.9, weight_decay=1e-5)
    scheduler = optim.lr_scheduler.LambdaLR(optimizer, lr_lambda = lambda epo: 0.9 ** epo)

    prev_time = datetime.now()
    for epo in range(max_epoch):
        train_loss = 0
        category_train_loss = 0
        confidence_train_loss = 0
        class_train_loss = 0
        instance_train_loss = 0
        height_train_loss = 0
        bcnn_model.train()
        for index, (in_feature, out_feature_gt) in enumerate(train_dataloader):
            out_feature_gt_np = out_feature_gt.detach().numpy().copy()
            pos_weight = out_feature_gt.detach().numpy().copy()
            pos_weight = pos_weight[:, 3, ...]
            object_idx = np.where(pos_weight == 0)
            nonobject_idx = np.where(pos_weight != 0)
            pos_weight[object_idx] = 1.0
            pos_weight[nonobject_idx] = 1.0
            pos_weight = torch.from_numpy(pos_weight)
            pos_weight = pos_weight.to(device)

            bike_class_weight = 8.0
            pedestrian_class_weight = 5.0
            object_weight = 1.0
            non_object_weight = 1.0
            class_weight = out_feature_gt.detach().numpy().copy()
            class_weight = class_weight[:, 4:5, ...]
            object_idx = np.where(class_weight != 0)
            nonobject_idx = np.where(class_weight == 0)
            class_weight[object_idx] = object_weight
            class_weight[nonobject_idx] = non_object_weight
            class_weight = np.concatenate(
                [class_weight,
                 class_weight,
                 class_weight * bike_class_weight,  # bike
                 class_weight * pedestrian_class_weight,  # pedestrian
                 class_weight,
                 class_weight], axis=1)
            class_weight = torch.from_numpy(class_weight)
            class_weight = class_weight.to(device)

            criterion = BcnnLoss().to(device)
            in_feature = in_feature.to(device)
            out_feature_gt = out_feature_gt.to(device)
            output = bcnn_model(in_feature)

            category_loss, confidence_loss, class_loss, instance_loss, height_loss\
                = criterion(output, in_feature, out_feature_gt, pos_weight, class_weight)
            loss = class_loss + (instance_loss + height_loss)


            optimizer.zero_grad()
            loss.backward()

            # loss_for_record = category_loss + confidence_loss + \
            #                   class_loss + instance_loss + height_loss
            loss_for_record = class_loss + instance_loss + height_loss
            iter_loss = loss_for_record.item()
            train_loss += iter_loss
            category_train_loss += category_loss.item()
            confidence_train_loss += confidence_loss.item()
            class_train_loss += class_loss.item()
            instance_train_loss += instance_loss.item()
            height_train_loss += height_loss.item()
            optimizer.step()

            confidence = output[0, 3:4, :, :]
            confidence_np = confidence.cpu().detach().numpy().copy()
            confidence_np = confidence_np.transpose(1, 2, 0)
            confidence_img = np.zeros((width, height, 1), dtype=np.uint8)

            # conf_idx = np.where(confidence_np[..., 0] > 0.5)
            conf_idx = np.where(
                confidence_np[..., 0] > confidence_np[..., 0].mean())
            confidence_img[conf_idx] = 1.0
            confidence_img = confidence_img.transpose(2, 0, 1)

            # draw pred class
            pred_class = output[0, 4:10, :, :]
            pred_class_np = pred_class.cpu().detach().numpy().copy()
            pred_class_np = pred_class_np.transpose(1, 2, 0)
            pred_class_np = np.argmax(pred_class_np, axis=2)[..., None]
            car_idx = np.where(pred_class_np[:, :, 0] == 1)
            bus_idx = np.where(pred_class_np[:, :, 0] == 2)
            bike_idx = np.where(pred_class_np[:, :, 0] == 3)
            human_idx = np.where(pred_class_np[:, :, 0] == 4)
            pred_class_img = np.zeros((width, height, 3))
            pred_class_img[car_idx] = [255, 0, 0]
            pred_class_img[bus_idx] = [0, 255, 0]
            pred_class_img[bike_idx] = [0, 0, 255]
            pred_class_img[human_idx] = [0, 255, 255]
            pred_class_img = pred_class_img.transpose(2, 0, 1)

            # draw label image
            out_feature_gt_np = out_feature_gt_np[0, ...].transpose(1, 2, 0)
            true_label_np = out_feature_gt_np[..., 4:10]
            true_label_np = np.argmax(true_label_np, axis=2)[..., None]
            car_idx = np.where(true_label_np[:, :, 0] == 1)
            bus_idx = np.where(true_label_np[:, :, 0] == 2)
            bike_idx = np.where(true_label_np[:, :, 0] == 3)
            human_idx = np.where(true_label_np[:, :, 0] == 4)
            label_img = np.zeros((width, height, 3))
            label_img[car_idx] = [255, 0, 0]
            label_img[bus_idx] = [0, 255, 0]
            label_img[bike_idx] = [0, 0, 255]
            label_img[human_idx] = [0, 255, 255]
            label_img = label_img.transpose(2, 0, 1)

            out_feature_gt_img \
                = out_feature_gt[0, 3:4, ...].cpu().detach().numpy().copy()

            in_feature_img = in_feature[0,
                                        non_empty_channle:non_empty_channle + 1,
                                        ...].cpu().detach().numpy().copy()
            in_feature_img[in_feature_img > 0] = 255

            if np.mod(index, vis_interval) == 0:
                print('epoch {}, {}/{},train loss is {}'.format(
                    epo,
                    index,
                    len(train_dataloader),
                    iter_loss))

                vis.images(in_feature_img,
                           win='train in_feature',
                           opts=dict(
                               title='train in_feature'))
                vis.images([out_feature_gt_img, confidence_img],
                           win='train_confidencena',
                           opts=dict(
                               title='train confidence(GT, Pred)'))
                vis.images([label_img, pred_class_img],
                           win='train_class',
                           opts=dict(
                               title='train class pred(GT, Pred)'))

            if index == train_data_num - 1:
                print("Finish train {} data. So start test.".format(index))
                break

        if len(train_dataloader) > 0:
            avg_train_loss = train_loss / len(train_dataloader)
            avg_confidence_train_loss = confidence_train_loss / \
                len(train_dataloader)
            avg_category_train_loss = category_train_loss / \
                len(train_dataloader)
            avg_class_train_loss = class_train_loss / len(train_dataloader)
            avg_instance_train_loss = instance_train_loss / \
                len(train_dataloader)
            avg_height_train_loss = height_train_loss / len(train_dataloader)
        else:
            avg_train_loss = train_loss
            avg_confidence_train_loss = confidence_train_loss
            avg_category_train_loss = category_train_loss
            avg_class_train_loss = class_train_loss
            avg_instance_train_loss = instance_train_loss
            avg_height_train_loss = height_train_loss

        vis.line(X=np.array([epo]), Y=np.array([avg_train_loss]), win='loss',
                 name='avg_train_loss', update='append')
        # vis.line(X=np.array([epo]), Y=np.array([avg_confidence_train_loss]), win='loss',
        #          name='avg_confidence_train_loss', update='append')
        # vis.line(X=np.array([epo]), Y=np.array([avg_category_train_loss]), win='loss',
        #          name='avg_category_train_loss', update='append')
        vis.line(
            X=np.array(
                [epo]),
            Y=np.array(
                [avg_class_train_loss]),
            win='loss',
            name='avg_class_train_loss',
            update='append')
        vis.line(
            X=np.array(
                [epo]),
            Y=np.array(
                [avg_instance_train_loss]),
            win='loss',
            name='avg_instance_train_loss',
            update='append')
        vis.line(
            X=np.array(
                [epo]),
            Y=np.array(
                [avg_height_train_loss]),
            win='loss',
            name='avg_height_train_loss',
            update='append')
        scheduler.step()

        test_loss = 0
        bcnn_model.eval()
        with torch.no_grad():
            for index, (in_feature, out_feature_gt) in enumerate(
                    test_dataloader):
                out_feature_gt_np = out_feature_gt.detach().numpy().copy()
                pos_weight = out_feature_gt.detach().numpy().copy()
                pos_weight = pos_weight[:, 3, ...]
                object_idx = np.where(pos_weight == 0)
                nonobject_idx = np.where(pos_weight != 0)
                pos_weight[object_idx] = 1.0
                pos_weight[nonobject_idx] = 1.0
                pos_weight = torch.from_numpy(pos_weight)
                pos_weight = pos_weight.to(device)

                class_weight = out_feature_gt.detach().numpy().copy()
                class_weight = class_weight[:, 4:5, ...]
                object_idx = np.where(class_weight != 0)
                nonobject_idx = np.where(class_weight == 0)
                class_weight[object_idx] = object_weight
                class_weight[nonobject_idx] = non_object_weight
                class_weight = np.concatenate(
                    [class_weight,
                     class_weight,
                     class_weight * bike_class_weight,  # bike
                     class_weight * pedestrian_class_weight,  # pedestrian
                     class_weight,
                     class_weight], axis=1)
                class_weight = torch.from_numpy(class_weight)
                class_weight = class_weight.to(device)

                in_feature = in_feature.to(device)
                out_feature_gt = out_feature_gt.to(device)

                optimizer.zero_grad()
                output = bcnn_model(in_feature)

                category_loss, confidence_loss, class_loss, instance_loss, height_loss\
                    = criterion(output, in_feature, out_feature_gt, pos_weight, class_weight)

                # loss_for_record = category_loss + confidence_loss + \
                #                   class_loss + instance_loss + height_loss
                loss_for_record = class_loss + instance_loss + height_loss
                iter_loss = loss_for_record.item()
                test_loss += iter_loss

                confidence = output[0, 3:4, :, :]
                confidence_np = confidence.cpu().detach().numpy().copy()
                confidence_np = confidence_np.transpose(1, 2, 0)
                confidence_img = np.zeros((width, height, 1), dtype=np.uint8)

                # conf_idx = np.where(confidence_np[..., 0] > 0.5)
                conf_idx = np.where(
                    confidence_np[..., 0] > confidence_np[..., 0].mean())
                confidence_img[conf_idx] = 1.0
                confidence_img = confidence_img.transpose(2, 0, 1)

                # draw pred class
                pred_class = output[0, 4:10, :, :]
                pred_class_np = pred_class.cpu().detach().numpy().copy()
                pred_class_np = pred_class_np.transpose(1, 2, 0)
                pred_class_np = np.argmax(pred_class_np, axis=2)[..., None]
                car_idx = np.where(pred_class_np[:, :, 0] == 1)
                bus_idx = np.where(pred_class_np[:, :, 0] == 2)
                bike_idx = np.where(pred_class_np[:, :, 0] == 3)
                human_idx = np.where(pred_class_np[:, :, 0] == 4)
                pred_class_img = np.zeros((width, height, 3))
                pred_class_img[car_idx] = [255, 0, 0]
                pred_class_img[bus_idx] = [0, 255, 0]
                pred_class_img[bike_idx] = [0, 0, 255]
                pred_class_img[human_idx] = [0, 255, 255]
                pred_class_img = pred_class_img.transpose(2, 0, 1)

                # draw label image
                out_feature_gt_np = out_feature_gt_np[0, ...].transpose(
                    1, 2, 0)
                true_label_np = out_feature_gt_np[..., 4:10]
                true_label_np = np.argmax(true_label_np, axis=2)[..., None]
                car_idx = np.where(true_label_np[:, :, 0] == 1)
                bus_idx = np.where(true_label_np[:, :, 0] == 2)
                bike_idx = np.where(true_label_np[:, :, 0] == 3)
                human_idx = np.where(true_label_np[:, :, 0] == 4)
                label_img = np.zeros((width, height, 3))
                label_img[car_idx] = [255, 0, 0]
                label_img[bus_idx] = [0, 255, 0]
                label_img[bike_idx] = [0, 0, 255]
                label_img[human_idx] = [0, 255, 255]
                label_img = label_img.transpose(2, 0, 1)

                out_feature_gt_img \
                    = out_feature_gt[0, 3:4, ...].cpu().detach().numpy().copy()

                in_feature_img \
                    = in_feature[0,
                                 non_empty_channle:non_empty_channle + 1,
                                 ...].cpu().detach().numpy().copy()

                if np.mod(index, vis_interval) == 0:
                    print('epoch {}, {}/{},test loss is {}'.format(
                        epo,
                        index,
                        len(test_dataloader),
                        iter_loss))
                    vis.images(in_feature_img,
                               win='test in_feature',
                               opts=dict(
                                   title='test in_feature'))
                    vis.images([out_feature_gt_img, confidence_img],
                               win='test_confidencena',
                               opts=dict(
                                   title='test confidence(GT, Pred)'))
                    vis.images([label_img, pred_class_img],
                               win='test_class',
                               opts=dict(
                                   title='test class pred(GT, Pred)'))
                if index == test_data_num - 1:
                    print("Finish test {} data".format(index))
                    break

            if len(test_dataloader) > 0:
                avg_test_loss = test_loss / len(test_dataloader)
            else:
                avg_test_loss = test_loss

        vis.line(X=np.array([epo]), Y=np.array([avg_train_loss]), win='loss',
                 name='avg_train_loss', update='append')
        vis.line(X=np.array([epo]), Y=np.array([avg_test_loss]), win='loss',
                 name='avg_test_loss', update='append')
        vis.line(
            X=np.array(
                [epo]),
            Y=np.array(
                [avg_class_train_loss]),
            win='loss',
            name='avg_class_train_loss',
            update='append')
        vis.line(
            X=np.array(
                [epo]),
            Y=np.array(
                [avg_instance_train_loss]),
            win='loss',
            name='avg_instance_train_loss',
            update='append')
        vis.line(
            X=np.array(
                [epo]),
            Y=np.array(
                [avg_height_train_loss]),
            win='loss',
            name='avg_height_train_loss',
            update='append')
        cur_time = datetime.now()
        h, remainder = divmod((cur_time - prev_time).seconds, 3600)
        m, s = divmod(remainder, 60)
        time_str = "Time %02d:%02d:%02d" % (h, m, s)
        prev_time = cur_time

        if np.mod(epo, save_model_interval) == 0:
            torch.save(bcnn_model.state_dict(),
                       'checkpoints/bcnn_latestmodel_' + now + '.pt')
        print('epoch train loss = %f, epoch test loss = %f, best_loss = %f, %s'
              % (train_loss / len(train_dataloader),
                 test_loss / len(test_dataloader),
                 best_loss,
                 time_str))
        if best_loss > test_loss / len(test_dataloader):
            print('update best model {} -> {}'.format(
                best_loss, test_loss / len(test_dataloader)))
            best_loss = test_loss / len(test_dataloader)
            torch.save(bcnn_model.state_dict(),
                       'checkpoints/bcnn_bestmodel_' + now + '.pt')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument(
        '--data_path',
        '-dp',
        type=str,
        help='Training data path',
        default='/media/kosuke/SANDISK/nusc/mini-6c-672-largelabel')
    parser.add_argument('--batch_size', '-bs', type=int,
                        help='max epoch',
                        default=1)
    parser.add_argument('--max_epoch', '-me', type=int,
                        help='max epoch',
                        default=1000000)
    parser.add_argument('--pretrained_model', '-p', type=str,
                        help='Pretrained model',
                        default='checkpoints/mini_672_6c.pt')
    parser.add_argument('--train_data_num', '-tr', type=int,
                        help='How much data to use for training',
                        default=1000000)
    parser.add_argument('--test_data_num', '-te', type=int,
                        help='How much data to use for testing',
                        default=1000000)
    parser.add_argument('--width', type=int,
                        help='feature map width',
                        default=672)
    parser.add_argument('--height', type=int,
                        help='feature map height',
                        default=672)
    parser.add_argument('--use_constant_feature', type=str,
                        help='Whether to use constant feature',
                        default=False)
    parser.add_argument('--use_intensity_feature', type=str,
                        help='Whether to use intensity feature',
                        default=True)

    args = parser.parse_args()
    train(data_path=args.data_path,
          batch_size=args.batch_size,
          max_epoch=args.max_epoch,
          pretrained_model=args.pretrained_model,
          train_data_num=args.train_data_num,
          test_data_num=args.test_data_num,
          width=args.width,
          height=args.height,
          use_constant_feature=args.use_constant_feature,
          use_intensity_feature=args.use_intensity_feature)
