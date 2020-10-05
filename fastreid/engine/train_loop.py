# encoding: utf-8
"""
credit:
https://github.com/facebookresearch/detectron2/blob/master/detectron2/engine/train_loop.py
"""

import logging
import time
import weakref
import os

import numpy as np
import torch
from torch.nn.parallel import DistributedDataParallel
from collections import OrderedDict
import fastreid.utils.comm as comm
from fastreid.utils.events import EventStorage
from fastreid.utils.file_io import PathManager
logger = logging.getLogger(__name__)
import copy
import math
from collections import Counter
from torch.autograd import Variable

from fastreid.utils.weight_init import weights_init_kaiming, weights_init_classifier

__all__ = ["HookBase", "TrainerBase", "SimpleTrainer"]


class HookBase:
    """
    Base class for hooks that can be registered with :class:`TrainerBase`.
    Each hook can implement 4 methods. The way they are called is demonstrated
    in the following snippet:
    .. code-block:: python
        hook.before_train()
        for iter in range(start_iter, max_iter):
            hook.before_step()
            trainer.run_step()
            hook.after_step()
        hook.after_train()
    Notes:
        1. In the hook method, users can access `self.trainer` to access more
           properties about the context (e.g., current iteration).
        2. A hook that does something in :meth:`before_step` can often be
           implemented equivalently in :meth:`after_step`.
           If the hook takes non-trivial time, it is strongly recommended to
           implement the hook in :meth:`after_step` instead of :meth:`before_step`.
           The convention is that :meth:`before_step` should only take negligible time.
           Following this convention will allow hooks that do care about the difference
           between :meth:`before_step` and :meth:`after_step` (e.g., timer) to
           function properly.
    Attributes:
        trainer: A weak reference to the trainer object. Set by the trainer when the hook is
            registered.
    """

    def before_train(self):
        """
        Called before the first iteration.
        """
        pass

    def after_train(self):
        """
        Called after the last iteration.
        """
        pass

    def before_step(self):
        """
        Called before each iteration.
        """
        pass

    def after_step(self):
        """
        Called after each iteration.
        """
        pass
class TrainerBase:
    """
    Base class for iterative trainer with hooks.
    The only assumption we made here is: the training runs in a loop.
    A subclass can implement what the loop is.
    We made no assumptions about the existence of dataloader, optimizer, model, etc.
    Attributes:
        iter(int): the current iteration.
        start_iter(int): The iteration to start with.
            By convention the minimum possible value is 0.
        max_iter(int): The iteration to end training.
        storage(EventStorage): An EventStorage that's opened during the course of training.
    """
    def __init__(self):
        self._hooks = []
    def register_hooks(self, hooks):
        """
        Register hooks to the trainer. The hooks are executed in the order
        they are registered.
        Args:
            hooks (list[Optional[HookBase]]): list of hooks
        """
        hooks = [h for h in hooks if h is not None]
        for h in hooks:
            assert isinstance(h, HookBase)
            # To avoid circular reference, hooks and trainer cannot own each other.
            # This normally does not matter, but will cause memory leak if the
            # involved objects contain __del__:
            # See http://engineering.hearsaysocial.com/2013/06/16/circular-references-in-python/
            h.trainer = weakref.proxy(self)
        self._hooks.extend(hooks)
    def train(self, start_iter: int, max_iter: int):
        """
        Args:
            start_iter, max_iter (int): See docs above
        """
        logger = logging.getLogger(__name__)
        logger.info("Starting training from iteration {}".format(start_iter))

        self.iter = self.start_iter = start_iter
        self.max_iter = max_iter

        with EventStorage(start_iter) as self.storage:
            self.before_train() # check hooks.py, engine/defaults.py
            for self.iter in range(start_iter, max_iter):
                self.before_step()
                if self.cfg.META.DATA.NAMES == '':
                    self.run_step()
                else:
                    self.run_step_meta_learning1()
                    self.run_step_meta_learning2()
                self.after_step()
            self.after_train()
    def before_train(self):
        for h in self._hooks:
            h.before_train()
    def after_train(self):
        for h in self._hooks:
            h.after_train()
    def before_step(self):
        for h in self._hooks:
            h.before_step()
    def after_step(self):
        for h in self._hooks:
            h.after_step()
        # this guarantees, that in each hook's after_step, storage.iter == trainer.iter
        self.storage.step()
    def run_step(self):
        raise NotImplementedError
class SimpleTrainer(TrainerBase):
    def __init__(self, cfg, model, data_loader, data_loader_add, optimizer, meta_param):
        super().__init__()
        self.model = model
        self.data_loader = data_loader

        if isinstance(data_loader, list):
            self._data_loader_iter = []
            for x in data_loader:
                self._data_loader_iter.append(iter(x))
        else:
            self._data_loader_iter = iter(data_loader)

        self.optimizer = optimizer
        self.meta_param = meta_param
        if cfg.SOLVER.AMP:
            self.scaler = torch.cuda.amp.GradScaler()
        else:
            self.scaler = None

        # additional setting
        self.bin_gates = [p for p in self.model.parameters() if getattr(p, 'bin_gate', False)]
        self.bin_names = [name for name, values in self.model.named_parameters() if getattr(values, 'bin_gate', False)]
        # Meta-leaning setting
        if len(self.meta_param) > 0:

            if data_loader_add['mtrain'] != None:
                self.data_loader_mtrain = data_loader_add['mtrain']
                if isinstance(self.data_loader_mtrain, list):
                    self._data_loader_iter_mtrain = []
                    for x in self.data_loader_mtrain:
                        self._data_loader_iter_mtrain.append(iter(x))
                else:
                    self._data_loader_iter_mtrain = iter(self.data_loader_mtrain)
            else:
                self.data_loader_mtrain = None
                self._data_loader_iter_mtrain = self._data_loader_iter

            if data_loader_add['mtest'] != None:
                self.data_loader_mtest = data_loader_add['mtest']
                if isinstance(self.data_loader_mtest, list):
                    self._data_loader_iter_mtest = []
                    for x in self.data_loader_mtest:
                        self._data_loader_iter_mtest.append(iter(x))
                else:
                    self._data_loader_iter_mtest = iter(self.data_loader_mtest)
            else:
                self.data_loader_mtest = None
                self._data_loader_iter_mtest = self._data_loader_iter_mtrain

            self.initial_requires_grad = self.grad_requires_init(model = self.model)
            find_group = ['layer1_conv_weight', 'layer1_conv_bias',
                          'layer1_bn_weight', 'layer1_bn_bias', 'layer1_bn_gate',
                          'classifier_fc_weight', 'classifier_fc_bias',
                          'classifier_norm_weight', 'classifier_norm_bias', 'classifier_norm_gate']
            new_group = list(self.cat_tuples(self.meta_param['meta_compute_layer'], self.meta_param['meta_update_layer']))
            find_group.extend(new_group)
            find_group = list(set(find_group))
            idx_group, dict_group = self.find_selected_optimizer(find_group, self.optimizer)
            self.idx_group = idx_group
            self.dict_group = dict_group
            self.inner_clamp = True
            self.print_flag = False

            # allocate whether each layer applies meta_learning
            self.all_layers = dict()
            for name, param in self.model.named_parameters():
                name = '.'.join(name.split('.')[:-1])
                raw_name = copy.copy(name)
                for i in range(10):
                    name = name.replace('.{}'.format(i), '[{}]'.format(i))
                exist_name = False
                for name_list in self.all_layers:
                    if name == name_list:
                        exist_name = True
                if not exist_name:
                    self.all_layers[name] = dict()
                    self.all_layers[name]['name'] = name
                    self.all_layers[name]['raw_name'] = raw_name

            for name, val in self.all_layers.items():
                self.all_layers[name]['w_param_idx'] = None
                self.all_layers[name]['b_param_idx'] = None
                self.all_layers[name]['g_param_idx'] = None
                for i, g in enumerate(self.optimizer.param_groups):
                    if val['raw_name'] + '.weight' == g['name']:
                        self.all_layers[name]['w_param_idx'] = i
                    if val['raw_name'] + '.bias' == g['name']:
                        self.all_layers[name]['b_param_idx'] = i
                    if val['raw_name'] + '.gate' == g['name']:
                        self.all_layers[name]['g_param_idx'] = i

            logger.info('[[Allocate compute_meta_params]]')
            new_object_name_params = 'compute_meta_params'
            new_object_name_gates = 'compute_meta_gates'
            for name, val in self.all_layers.items():
                flag_meta_params = False
                flag_meta_gates = False
                for update_name in self.meta_param['meta_compute_layer']:
                    if 'gate' in update_name:
                        split_update_name = update_name.split('_')
                        if len(split_update_name) == 1:  # gates of all bn layers
                            if 'bn' in name:
                                flag_meta_gates = True # all bn layers
                        else:
                            flag_splits = np.zeros(len(split_update_name))
                            for i, splits in enumerate(split_update_name):
                                if splits in name:
                                    flag_splits[i] = 1
                            if sum(flag_splits) >= len(split_update_name) - 1:
                                flag_meta_gates = True
                        if flag_meta_gates:
                            break
                for update_name in self.meta_param['meta_compute_layer']:
                    if 'gate' not in update_name:
                        split_update_name = update_name.split('_')
                        flag_splits = np.zeros(len(split_update_name), dtype=bool)
                        for i, splits in enumerate(split_update_name):
                            if splits in name:
                                flag_splits[i] = True
                        flag_meta_params = all(flag_splits)
                        if flag_meta_params:
                            break
                if flag_meta_params:
                    logger.info('{} is in the {}'.format(update_name, name))
                    exec('self.model.{}.{} = {}'.format(name, new_object_name_params, True))
                else:
                    exec('self.model.{}.{} = {}'.format(name, new_object_name_params, False))

                if flag_meta_gates:
                    logger.info('{} is in the {}'.format(update_name, name))
                    exec('self.model.{}.{} = {}'.format(name, new_object_name_gates, True))
                else:
                    exec('self.model.{}.{} = {}'.format(name, new_object_name_gates, False))

            logger.info('[[Exceptions 1]]')
            name = 'backbone.conv1'; update_name = 'layer0_conv'
            if update_name in self.meta_param['meta_compute_layer']:
                logger.info('{} is in the {}'.format(update_name, name))
                exec('self.model.{}.{} = {}'.format(name, new_object_name_params, True))
            name = 'backbone.conv1'; update_name = 'layer0'
            if update_name in self.meta_param['meta_compute_layer']:
                logger.info('{} is in the {}'.format(update_name, name))
                exec('self.model.{}.{} = {}'.format(name, new_object_name_params, True))
            name = 'backbone.bn1'; update_name = 'layer0_bn'
            if update_name in self.meta_param['meta_compute_layer']:
                logger.info('{} is in the {}'.format(update_name, name))
                exec('self.model.{}.{} = {}'.format(name, new_object_name_params, True))
            name = 'backbone.bn1'; update_name = 'layer0'
            if update_name in self.meta_param['meta_compute_layer']:
                logger.info('{} is in the {}'.format(update_name, name))
                exec('self.model.{}.{} = {}'.format(name, new_object_name_params, True))
                exec('self.model.{}.{} = {}'.format(name, new_object_name_gates, True))
            name = 'backbone.bn1'; update_name = 'layer0_bn_gate'
            if update_name in self.meta_param['meta_compute_layer']:
                logger.info('{} is in the {}'.format(update_name, name))
                exec('self.model.{}.{} = {}'.format(name, new_object_name_gates, True))

            # logger.info('[[Exceptions 2]]')
            # for name, val in self.all_layers.items():
            #     if 'downsample[0]' in name:
            #         name2 = '.'.join(name.split('.')[:-1]) + '.conv1'
            #         exec('self.model.{}.{} = self.model.{}.{}'.format(
            #             name, new_object_name_params, name2, new_object_name_params))
            #         logger.info('copy [{}.{}] <- [{}.{}]'.format(
            #             name, new_object_name_params, name2, new_object_name_params))
            #     if 'downsample[1]' in name:
            #         name2 = '.'.join(name.split('.')[:-1]) + '.bn1'
            #         exec('self.model.{}.{} = self.model.{}.{}'.format(
            #             name, new_object_name_params, name2, new_object_name_params))
            #         logger.info('copy [{}.{}] <- [{}.{}]'.format(
            #             name, new_object_name_params, name2, new_object_name_params))
            #         exec('self.model.{}.{} = self.model.{}.{}'.format(
            #             name, new_object_name_gates, name2, new_object_name_gates))
            #         logger.info('copy [{}.{}] <- [{}.{}]'.format(
            #             name, new_object_name_gates, name2, new_object_name_gates))

            # layer4.0.downsample.0.weight
            # layer4.0.downsample.1.{running_mean, running_var, weight, bias}
            #
            # layer4.0.downsample.conv1.weight
            # layer4.0.downsample.bn1.{running_mean, running_var, weight, bias}

            for name, val in self.all_layers.items():
                exec("val['{}'] = self.model.{}.{}".format(new_object_name_params, name, new_object_name_params))
                exec("val['{}'] = self.model.{}.{}".format(new_object_name_gates, name, new_object_name_gates))

            logger.info('[[Summary]]')
            logger.info('Meta compute layer : {}'.format(self.meta_param['meta_compute_layer']))
            for name, val in self.all_layers.items():
                logger.info('Name: {}, meta_param: {}, meta_gate: {}'.format(name, val[new_object_name_params], val[new_object_name_gates]))
        else:
            find_group = ['layer1_conv_weight', 'layer1_conv_bias',
                          'layer1_bn_weight', 'layer1_bn_bias', 'layer1_bn_gate',
                          'classifier_fc_weight', 'classifier_fc_bias',
                          'classifier_norm_weight', 'classifier_norm_bias', 'classifier_norm_gate']
            idx_group, dict_group = self.find_selected_optimizer(find_group, self.optimizer)
            self.idx_group = idx_group
            self.dict_group = dict_group
            self.inner_clamp = True
            self.print_flag = False

    def run_step(self):
        # initial setting
        # self.optimizer.zero_grad()
        assert self.model.training, "[SimpleTrainer] model was changed to eval mode!"
        metrics_dict = dict()

        self.print_selected_optimizer('0) start', self.idx_group, self.optimizer, True)

        # Load dataset
        data, data_time = self.get_data(self._data_loader_iter, list_sample = None)

        # Training (forward & backward)
        opt = self.opt_setting('basic') # option
        losses, loss_dict = self.basic_forward(data, self.model, opt) # forward
        self.basic_backward(losses, self.optimizer) # backward

        # Post-processing
        for name, val in loss_dict.items(): metrics_dict[name] = val
        metrics_dict["data_time"] = data_time
        self._write_metrics(metrics_dict)
        # if self.iter % (self.cfg.SOLVER.WRITE_PERIOD_PARAM * self.cfg.SOLVER.WRITE_PERIOD) == 0:
        #     self.logger_parameter_info(self.model)
    def run_step_meta_learning1(self):

        # initial setting
        assert self.model.training, "[SimpleTrainer] model was changed to eval mode!"
        self.metrics_dict = dict()
        # 1) Meta-initialization
        name_loss = '1)'
        opt = self.opt_setting('basic')
        # self.grad_requires_check(self.model)
        cnt_init = 0
        self.data_time_all = 0.0
        while(cnt_init < self.meta_param['iter_init_inner']):
            self.grad_setting('basic')  # Freeze "meta_update_layer"
            # torch.cuda.empty_cache() # 1.5ms
            # self.optimizer.zero_grad()
            self.print_selected_optimizer('0) start', self.idx_group, self.optimizer, self.meta_param['detail_mode'])
            cnt_init += 1
            data, data_time = self.get_data(self._data_loader_iter, list_sample = None)
            self.data_time_all += data_time
            losses, loss_dict = self.basic_forward(data, self.model, opt) # forward
            self.basic_backward(losses, self.optimizer) # backward
            for name, val in loss_dict.items():
                t = name_loss+name
                self.metrics_dict[t] = self.metrics_dict[t] + val if t in self.metrics_dict.keys() else val
            self.print_selected_optimizer('1) after meta-init', self.idx_group, self.optimizer,
                                          self.meta_param['detail_mode'])

            if self.meta_param['flag_manual_zero_grad'] != 'hold':
                self.manual_zero_grad(self.model)
                # self.optimizer.zero_grad()
            if self.meta_param['flag_manual_memory_empty']:
                torch.cuda.empty_cache()
            if self.meta_param['sync']: torch.cuda.synchronize()

        for name in self.metrics_dict.keys():
            if name_loss in name: self.metrics_dict[name] /= float(self.meta_param['iter_init_inner'])


    def run_step_meta_learning2(self):

        # start1 = time.perf_counter()
        # data_time = time.perf_counter() - start

        # Meta-learning
        cnt_global = 0
        self.print_selected_optimizer('2) before meta-train', self.idx_group, self.optimizer,
                                      self.meta_param['detail_mode'])
        while(cnt_global < self.meta_param['iter_init_outer']):
            mtrain_losses = []
            mtest_losses = []
            cnt_global += 1
            cnt_local = 0
            while(cnt_local < self.meta_param['iter_mtrain']):
                if self.meta_param['shuffle_domain'] or \
                        (not self.meta_param['shuffle_domain'] and cnt_local == 0):
                    list_all = np.random.permutation(self.meta_param['num_domain'])
                    list_mtrain = list(list_all[0:self.meta_param['num_mtrain']])
                    list_mtest = list(list_all[self.meta_param['num_mtrain']:
                                               self.meta_param['num_mtrain'] + self.meta_param['num_mtest']])

                # 2) Meta-train
                cnt_local += 1
                name_loss_mtrain = '2)'
                opt = self.opt_setting('mtrain')

                if self.data_loader_mtest is None:
                    if self.meta_param['whole']:
                        data, data_time = self.get_data(self._data_loader_iter_mtrain, list_sample = list_mtrain, opt = 'all')
                        self.data_time_all += data_time
                        data_mtrain = data[0]
                        data_mtest = data[1]
                    else:
                        data_mtrain, data_time = self.get_data(self._data_loader_iter_mtrain, list_sample=list_mtrain)
                        self.data_time_all += data_time
                        data_mtest, data_time = self.get_data(self._data_loader_iter_mtrain, list_sample=list_mtest)
                        self.data_time_all += data_time
                else:
                    data_mtrain, data_time = self.get_data(self._data_loader_iter_mtrain, list_sample = list_mtrain)
                    self.data_time_all += data_time
                    data_mtest, data_time = self.get_data(self._data_loader_iter_mtest, list_sample = list_mtest)
                    self.data_time_all += data_time

                self.grad_setting('mtrain_both')
                losses, loss_dict = self.basic_forward(data_mtrain, self.model, opt) # forward
                # del data, opt
                mtrain_losses.append(losses)
                for name, val in loss_dict.items():
                    t = name_loss_mtrain + name
                    self.metrics_dict[t] = self.metrics_dict[t] + val if t in self.metrics_dict.keys() else val

                self.optimizer.zero_grad()
                # 3) Meta-test
                name_loss_mtest = '3)'
                self.grad_setting('mtrain_single') # melt only meta_compute parameters
                opt = self.opt_setting('mtest', losses) # auto_grad based on requires_grad of model
                self.grad_setting('mtrain_both') # melt both meta_compute and meta_update parameters
                losses, loss_dict = self.basic_forward(data_mtest, self.model, opt) # forward
                # del data, opt
                # check auto_grad Variable == self.model.~~.compute_meta_gates + compute_meta_params

                # self.optimizer.zero_grad()
                mtest_losses.append(losses)
                for name, val in loss_dict.items():
                    t = name_loss_mtest + name
                    self.metrics_dict[t] = self.metrics_dict[t] + val if t in self.metrics_dict.keys() else val

            self.grad_setting('mtest')
            for name in self.metrics_dict.keys():
                if (name_loss_mtest in name) or (name_loss_mtrain in name):
                    self.metrics_dict[name] /= float(self.meta_param['iter_init_outer'])

            if self.meta_param['iter_init_outer'] == 1:
                mtrain_losses = mtrain_losses[0]
                mtest_losses = mtest_losses[0]
            else:
                mtrain_losses = torch.sum(torch.stack(mtrain_losses))
                mtest_losses = torch.sum(torch.stack(mtest_losses))

            if self.meta_param['loss_combined']:
                total_losses = self.meta_param['loss_weight'] * mtrain_losses + mtest_losses
            else:
                total_losses = mtest_losses
            total_losses /= float(self.meta_param['iter_mtrain'])

            self.basic_backward(total_losses, self.optimizer) # backward

            # del total_losses, mtrain_losses, mtest_losses
            if self.meta_param['flag_manual_zero_grad'] != 'hold':
                self.manual_zero_grad(self.model)
                self.optimizer.zero_grad()
            if self.meta_param['flag_manual_memory_empty']:
                torch.cuda.empty_cache()
            if self.meta_param['sync']: torch.cuda.synchronize()

            self.print_selected_optimizer('2) after meta-learning', self.idx_group, self.optimizer, self.meta_param['detail_mode'])
        self.metrics_dict["data_time"] = self.data_time_all
        self._write_metrics(self.metrics_dict)
        # self.storage.put_scalar

        with torch.no_grad():
            if len(self.bin_names) > 0 and (self.iter + 1) % (self.cfg.SOLVER.WRITE_PERIOD_BIN) == 0:
                start = time.perf_counter()
                all_gate_dict = dict()
                for j in range(len(self.bin_names)):
                    name = '_'.join(self.bin_names[j].split('.')[1:]).\
                        replace('bn', 'b').replace('gate','g').replace('layer', 'l').replace('conv','c')
                    val_mean = torch.mean(self.bin_gates[j].data).tolist()
                    val_std = torch.std(self.bin_gates[j].data).tolist()
                    val_hist = torch.histc(self.bin_gates[j].data, bins=20, min=0.0, max=1.0).int()
                    all_gate_dict[name + '_mean']= val_mean
                    all_gate_dict[name + '_std']= val_std
                    for x in torch.nonzero(val_hist.data):
                        all_gate_dict[name + '_hist' + str(x[0].tolist())] = val_hist[x[0]].tolist()
                    # all_gate_dict['hist_' + name]= str(val_hist.tolist()).replace(' ','')
                self.storage.put_scalars(**all_gate_dict, smoothing_hint=False)
                # print(time.perf_counter() - start)


        # print("Data loading time: {}".format(self.data_time_all))

        # self.logger_parameter_info(self.model)
    def get_data(self, data_loader_iter, list_sample = None, opt = None):
        start = time.perf_counter()
        if data_loader_iter is not None:
            data = None
            while(data == None):
                if isinstance(data_loader_iter, list):
                    if list_sample is None:
                        data = self.data_aggregation(dataloader = data_loader_iter, list_num = [x for x in range(len(data_loader_iter))])
                    else:
                        data = self.data_aggregation(dataloader = data_loader_iter, list_num = [x for x in list_sample])

                else:
                    data = next(data_loader_iter)
                    if list_sample != None:
                        domain_idx = data['others']['domains']
                        cnt = 0
                        for sample in list_sample:
                            if cnt == 0:
                                t_logical_domain = domain_idx == sample
                            else:
                                t_logical_domain += domain_idx == sample
                            cnt += 1

                        # data1
                        if int(sum(t_logical_domain)) == 0:
                            data = None
                            logger.info('No data including list_domain')
                        else:
                            data1 = dict()
                            for name, value in data.items():
                                if torch.is_tensor(value):
                                    data1[name] = data[name][t_logical_domain]
                                elif isinstance(value, dict):
                                    data1[name] = dict()
                                    for name_local, value_local in value.items():
                                        if torch.is_tensor(value_local):
                                            data1[name][name_local] = data[name][name_local][t_logical_domain]
                                elif isinstance(value, list):
                                    data1[name] = [x for i, x in enumerate(data[name]) if t_logical_domain[i]]

                        # data2 (if opt == 'all')
                        if opt == 'all':
                            t_logical_domain_reversed = t_logical_domain == False
                            if int(sum(t_logical_domain_reversed)) == 0:
                                data2 = None
                                logger.info('No data including list_domain')
                            else:
                                data2 = dict()
                                for name, value in data.items():
                                    if torch.is_tensor(value):
                                        data2[name] = data[name][t_logical_domain_reversed]
                                    elif isinstance(value, dict):
                                        data2[name] = dict()
                                        for name_local, value_local in value.items():
                                            if torch.is_tensor(value_local):
                                                data2[name][name_local] = data[name][name_local][t_logical_domain_reversed]
                                    elif isinstance(value, list):
                                        data2[name] = [x for i, x in enumerate(data[name]) if t_logical_domain_reversed[i]]
                            data = [data1, data2]
                        else:
                            data = data1
        else:
            data = None
            logger.info('No data including list_domain')

        data_time = time.perf_counter() - start
                # sample data

        return data, data_time
    def basic_forward(self, data, model, opt = None):
        model = model.module if isinstance(model, DistributedDataParallel) else model
        if data != None:
            with torch.cuda.amp.autocast(enabled=self.scaler is not None):
                outs = model(data, opt)
                loss_dict = model.losses(outs, opt)
                losses = sum(loss_dict.values())
            self._detect_anomaly(losses, loss_dict)
        else:
            losses = None
            loss_dict = dict()
        return losses, loss_dict
    def basic_backward(self, losses, optimizer):
        if losses != None:
            optimizer.zero_grad()
            if self.scaler is None:
                losses.backward()
                optimizer.step()
            else:
                self.scaler.scale(losses).backward()
                self.scaler.step(optimizer)
                self.scaler.update()
            for p in self.bin_gates:
                p.data.clamp_(min=0, max=1)

            # start = time.perf_counter()
            # processing_time = time.perf_counter() - start
            # print('sync time: {}'.format(processing_time))
    def opt_setting(self, flag, losses = None):
        if flag == 'basic':
            opt = {}
            opt['param_update'] = False
            opt['loss'] = self.cfg['MODEL']['LOSSES']['NAME']
            try:
                opt['type_running_stats'] = self.meta_param['type_running_stats_init']
            except:
                opt['type_running_stats'] = 'general'
        elif flag == 'mtrain':
            opt = {}
            opt['param_update'] = False
            opt['loss'] = self.meta_param['loss_name_mtrain']
            opt['type_running_stats'] = self.meta_param['type_running_stats_mtrain']
        elif flag == 'mtest':
            opt = {}
            opt['param_update'] = True
            opt['loss'] = self.meta_param['loss_name_mtest']
            opt['use_second_order'] = self.meta_param['use_second_order']
            opt['stop_gradient'] = self.meta_param['stop_gradient']
            opt['allow_unused'] = self.meta_param['allow_unused']
            opt['zero_grad'] = self.meta_param['zero_grad']
            opt['type_running_stats'] = self.meta_param['type_running_stats_mtest']

            # self.meta_param['update_cyclic_ratio']
            # self.meta_param['update_cyclic_period']
            if self.meta_param['update_ratio'] == 0.0:
                print('.')
                # self.meta_param['iters_per_epoch']
                one_period = self.meta_param['iters_per_epoch'] / self.meta_param['update_cyclic_period']
                b = math.log10(self.meta_param['update_cyclic_ratio'])
                a = b / (one_period/4.0*1.0)
                # for i in range(self.meta_param['iters_per_epoch']):
                rem_val = self.iter % one_period
                if  rem_val < (one_period/4.0*1.0): # 1st period
                    meta_ratio = a * rem_val # y = ax
                elif  rem_val < (one_period/4.0*2.0): # 2nd period
                    rem_val -= one_period/4.0*1.0
                    meta_ratio = b - a * rem_val # y = b - ax
                elif  rem_val < (one_period/4.0*3.0): # 3rd period
                    rem_val -= one_period/4.0*2.0
                    meta_ratio = - a * rem_val # y = - ax
                else: # 4th period
                    rem_val -= one_period/4.0*3.0
                    meta_ratio = - b + a * rem_val # y = -b + ax
                meta_ratio = pow(10, meta_ratio)

                # print(meta_ratio)
            else:
                meta_ratio = self.meta_param['update_ratio']

            for name, val in self.all_layers.items():
                if self.all_layers[name]['w_param_idx'] is not None:
                    self.all_layers[name]['w_step_size'] = \
                        self.optimizer.param_groups[self.all_layers[name]['w_param_idx']]["lr"]\
                        * meta_ratio
                else:
                    self.all_layers[name]['b_step_size'] = None

                if self.all_layers[name]['b_param_idx'] is not None:
                    self.all_layers[name]['b_step_size'] = \
                        self.optimizer.param_groups[self.all_layers[name]['b_param_idx']]["lr"]\
                        * meta_ratio
                else:
                    self.all_layers[name]['b_step_size'] = None

                if self.all_layers[name]['g_param_idx'] is not None:
                    self.all_layers[name]['g_step_size'] = \
                        self.optimizer.param_groups[self.all_layers[name]['g_param_idx']]["lr"]\
                        * meta_ratio
                else:
                    self.all_layers[name]['g_step_size'] = None

            for name, val in self.all_layers.items():
                if val['compute_meta_params']:
                    exec('self.model.{}.{} = {}'.format(name, 'w_step_size', val['w_step_size']))
                    exec('self.model.{}.{} = {}'.format(name, 'b_step_size', val['b_step_size']))
                if val['compute_meta_gates']:
                    exec('self.model.{}.{} = {}'.format(name, 'g_step_size', val['g_step_size']))

            opt['auto_grad_outside'] = self.meta_param['auto_grad_outside']

            # inner
            if opt['auto_grad_outside']:
                # outer
                names_weights_copy = dict()
                for name, param in self.model.named_parameters():
                    if param.requires_grad:
                        names_weights_copy['self.model.' + name] = param
                        if param.grad is not None and opt['zero_grad']:
                            if torch.sum(param.grad) > 0:
                                param.grad.zero_()

                if self.scaler is not None:
                    grad_params = torch.autograd.grad(
                        self.scaler.scale(losses), names_weights_copy.values(),
                        create_graph=opt['use_second_order'], allow_unused=opt['allow_unused'])
                else:
                    grad_params = torch.autograd.grad(
                        losses, names_weights_copy.values(),
                        create_graph=opt['use_second_order'], allow_unused=opt['allow_unused'])

                # for i in range(len(grad_params)):
                #     grad_params[i][torch.isnan(grad_params[i])] = 1.0
                if opt['stop_gradient']:
                    grad_params = list(grad_params)
                    for i in range(len(grad_params)):
                        grad_params[i] = Variable(grad_params[i].data, requires_grad=False)

                if self.scaler is not None:
                    inv_scale = 1. / self.scaler.get_scale()
                    opt['grad_params'] = [p * inv_scale for p in grad_params]
                else:
                    opt['grad_params'] = [p for p in grad_params]
                opt['meta_loss'] = None
            else:
                opt['meta_loss'] = losses

                # outer update
                # names_weights_copy = dict()
                # for name, param in self.model.named_parameters():
                #     if param.requires_grad:
                #         names_weights_copy['self.model.' + name] = param
                # scaled_grad_params = torch.autograd.grad(
                #     self.scaler.scale(losses), names_weights_copy.values(),
                #     create_graph=opt['use_second_order'], allow_unused=opt['allow_unused'])
                # inv_scale = 1. / self.scaler.get_scale()
                # scaled_grad_params = [p * inv_scale for p in scaled_grad_params]
                # names_weights_copy = [p for p in names_weights_copy.values()]
                # opt['grad_params'] = []
                # for i in range(len(scaled_grad_params)):
                #     opt['grad_params'].append(names_weights_copy[i] - 0.0001 * scaled_grad_params[i])
                # opt['meta_loss'] = None


        return opt
    def grad_setting(self, flag):


        if flag == 'basic':
            self.grad_requires_remove(
                model = self.model,
                ori_grad = self.initial_requires_grad,
                freeze_target = self.meta_param['meta_update_layer'],
                reverse_flag = False, # True: freeze target / False: freeze w/o target
                print_flag = self.print_flag)
        elif flag == 'mtrain_both':
            if self.meta_param['freeze_gradient_meta']:
                self.grad_requires_remove(
                    model = self.model,
                    ori_grad = self.initial_requires_grad,
                    freeze_target = self.cat_tuples(self.meta_param['meta_update_layer'], self.meta_param['meta_compute_layer']),
                    # freeze_target = self.meta_param['meta_compute_layer'],
                    reverse_flag = True, # True: freeze target / False: freeze w/o target
                    print_flag = self.print_flag)
            else:
                self.grad_requires_recover(model=self.model, ori_grad=self.initial_requires_grad)

        elif flag == 'mtrain_single':
            if self.meta_param['freeze_gradient_meta']:
                self.grad_requires_remove(
                    model = self.model,
                    ori_grad = self.initial_requires_grad,
                    # freeze_target = self.cat_tuples(self.meta_param['meta_update_layer'], self.meta_param['meta_compute_layer']),
                    freeze_target = self.meta_param['meta_compute_layer'],
                    reverse_flag = True, # True: freeze target / False: freeze w/o target
                    print_flag = self.print_flag)
            else:
                self.grad_requires_recover(model=self.model, ori_grad=self.initial_requires_grad)

        elif flag == 'mtest':
            self.grad_requires_remove(
                model=self.model,
                ori_grad=self.initial_requires_grad,
                freeze_target=self.meta_param['meta_update_layer'],
                reverse_flag=True, # True: freeze target / False: freeze w/o target
                print_flag=self.print_flag)
    def find_selected_optimizer(self, find_group, optimizer):
        # find parameter, lr, required_grad, shape
        logger.info('Storage parameter, lr, requires_grad, shape! in {}'.format(find_group))
        idx_group = []
        dict_group = dict()
        for j in range(len(find_group)):
            idx_local = []
            for i, x in enumerate(optimizer.param_groups):
                split_find_group = find_group[j].split('_')
                flag_splits = np.zeros(len(split_find_group), dtype=bool)
                for k, splits in enumerate(split_find_group):
                    if splits in x['name']:
                        flag_splits[k] = True
                flag_target = all(flag_splits)
                if flag_target:
                    dict_group[x['name']] = i
                    idx_local.append(i)
            if len(idx_local) > 0:
                logger.info('Find {} in {}'.format(find_group[j], optimizer.param_groups[idx_local[0]]['name']))
                idx_group.append(idx_local[0])
            else:
                logger.info('error in find_group')
        idx_group = list(set(idx_group))
        return idx_group, dict_group
    def print_selected_optimizer(self, txt, idx_group, optimizer, detail_mode):
        if detail_mode and (self.iter <= 5 or self.iter % 100 == 0):
            num_float = 8
            for x in idx_group:
                t_name = optimizer.param_groups[x]['name']
                t_param = optimizer.param_groups[x]['params'][0].view(-1)[0]
                t_lr = optimizer.param_groups[x]['lr']
                t_grad = optimizer.param_groups[x]['params'][0].requires_grad
                t_grad_val = optimizer.param_groups[x]['params'][0].grad
                if t_grad_val != None:
                    if torch.sum(t_grad_val) == 0:
                        t_grad_val = 'Zero'
                    else:
                        t_grad_val = 'Exist'
                # t_shape = optimizer.param_groups[x]['params'][0].shape
                for name, param in self.model.named_parameters():
                    if name == t_name:
                        m_param = param.view(-1)[0]
                        m_grad = param.requires_grad
                        m_grad_val = param.grad
                        if m_grad_val != None:
                            if torch.sum(m_grad_val) == 0:
                                m_grad_val = 'Zero'
                            else:
                                m_grad_val = 'Exist'
                        val = torch.sum(param - optimizer.param_groups[x]['params'][0])
                        break
                if float(val) != 0:
                    logger.info('*****')
                    logger.info('<=={}==>[optimizer] --> [{}], w:{}, grad:{}, lr:{}'.format(txt, t_name, round(float(t_param), num_float), t_grad, t_lr))
                    logger.info('<=={}==>[self.model] --> [{}], w:{}, grad:{}'.format(txt, t_name, round(float(m_param), num_float), m_grad))
                    logger.info('*****')
                else:
                    logger.info('[**{}**] --> [{}], w:{}, requires_grad:{}, opt_grad:{}, prm_grad:{}, lr:{}'.format(txt, t_name, round(float(t_param), num_float), t_grad, t_grad_val, m_grad_val, t_lr))
    # def logger_parameter_info(self, model):
    #     with torch.no_grad():
    #         write_dict = dict()
    #         round_num = 4
    #         name_num = 20
    #         for name, param in model.named_parameters():  # only update regularizer
    #             if 'reg' in name:
    #                 name = '_'.join([x[:name_num] for x in name.split('.')[1:]])
    #                 name = name + '+'
    #                 write_dict[name] = round(float(torch.sum(param.data.view(-1) > 0)) / len(param.data.view(-1)),
    #                                          round_num)
    #
    #         for name, param in model.named_parameters():
    #             if ('meta' in name) and ('fc' in name) and ('weight' in name) and (not 'domain' in name):
    #                 name = '_'.join([x[:name_num] for x in name.split('.')[1:]])
    #                 # name_std = name + '_std'
    #                 # write_dict[name_std] = round(float(torch.std(param.data.view(-1))), round_num)
    #                 # name_mean = name + '_mean'
    #                 # write_dict[name_mean] = round(float(torch.mean(param.data.view(-1))), round_num)
    #                 name_std10 = name + '_std10'
    #                 ratio = 0.1
    #                 write_dict[name_std10] = round(
    #                     float(torch.sum((param.data.view(-1) > - ratio * float(torch.std(param.data.view(-1)))) * (
    #                             param.data.view(-1) < ratio * float(torch.std(param.data.view(-1)))))) / len(
    #                         param.data.view(-1)), round_num)
    #
    #         for name, param in model.named_parameters():
    #             if ('gate' in name) and (not 'domain' in name):
    #                 name = '_'.join([x[:name_num] for x in name.split('.')[1:]])
    #                 name_mean = name + '_mean'
    #                 write_dict[name_mean] = round(float(torch.mean(param.data.view(-1))), round_num)
    #         logger.info(write_dict)
    def grad_requires_init(self, model):

        out_requires_grad = dict()
        for name, param in model.named_parameters():
            out_requires_grad[name] = param.requires_grad
        return out_requires_grad
    def grad_requires_check(self, model):

        out_requires_grad = dict()
        for name, param in model.named_parameters():
            logger.info("[{}], grad: [{}]".format(name, param.requires_grad))
        return out_requires_grad
    def grad_requires_remove(self, model, ori_grad, freeze_target, reverse_flag = False, print_flag = False):

        if reverse_flag: # freeze layers w/o target layers
            for name, param in model.named_parameters():

                flag_freeze = True
                for freeze_name in list(freeze_target):
                    split_freeze_name = freeze_name.split('_')
                    if 'gate' in name:
                        if 'gate' not in split_freeze_name:
                            continue
                    else: # 'weight' / 'bais
                        if 'gate' in split_freeze_name:
                            continue
                    flag_splits = np.zeros(len(split_freeze_name), dtype=bool)
                    for i, splits in enumerate(split_freeze_name):
                        if splits in name:
                            flag_splits[i] = True
                    flag_target = all(flag_splits)
                    if flag_target:
                        flag_freeze = False
                        break

                if flag_freeze:
                    param.requires_grad = False
                    if print_flag: print("freeze '{}' layer's grad".format(name))
                else:
                    param.requires_grad = ori_grad[name]
        else: # freeze layers based on target
            for name, param in model.named_parameters():

                flag_freeze = False
                for freeze_name in list(freeze_target):
                    split_freeze_name = freeze_name.split('_')
                    if 'gate' in name:
                        if 'gate' not in split_freeze_name:
                            continue
                    else: # 'weight' / 'bais
                        if 'gate' in split_freeze_name:
                            continue
                    flag_splits = np.zeros(len(split_freeze_name), dtype=bool)
                    for i, splits in enumerate(split_freeze_name):
                        if splits in name:
                            flag_splits[i] = True
                    flag_target = all(flag_splits)
                    if flag_target:
                        flag_freeze = True
                        break

                if flag_freeze:
                    param.requires_grad = False
                    if print_flag: print("freeze '{}' layer's grad".format(name))
                else:
                    param.requires_grad = ori_grad[name]
    def grad_requires_recover(self, model, ori_grad):

        # recover gradient requirements
        for name, param in model.named_parameters():
            param.requires_grad = ori_grad[name]
    def grad_val_remove(self, model, freeze_target, reverse_flag = False, print_flag = False):
        if reverse_flag: # remove grad w/o target layers
            for name, param in model.named_parameters():
                if param.grad is not None:

                    flag_remove = True
                    for remove_name in list(freeze_target):
                        split_remove_name = remove_name.split('_')
                        flag_splits = np.zeros(len(split_remove_name), dtype=bool)
                        for i, splits in enumerate(split_remove_name):
                            if splits in name:
                                flag_splits[i] = True
                        flag_target = all(flag_splits)
                        if flag_target:
                            flag_remove = False
                            break

                    if flag_remove:
                        param.grad = None
                        if print_flag:
                            print("remove '{}' layer's grad".format(name))
        else: # remove grad based on target layers
            for name, param in model.named_parameters():
                if param.grad is not None:

                    flag_remove = False
                    for remove_name in list(freeze_target):
                        split_remove_name = remove_name.split('_')
                        flag_splits = np.zeros(len(split_remove_name), dtype=bool)
                        for i, splits in enumerate(split_remove_name):
                            if splits in name:
                                flag_splits[i] = True
                        flag_target = all(flag_splits)
                        if flag_target:
                            flag_remove = True
                            break

                    if flag_remove:
                        param.grad = None
                        if print_flag:
                            print("remove '{}' layer's grad".format(name))
    def data_aggregation(self, dataloader, list_num):
        data = None
        for cnt, list_idx in enumerate(list_num):
            if cnt == 0:
                data = next(dataloader[list_idx])
            else:
                for name, value in next(dataloader[list_idx]).items():
                    if torch.is_tensor(value):
                        data[name] = torch.cat((data[name], value), 0)
                    elif isinstance(value, dict):
                        for name_local, value_local in value.items():
                            if torch.is_tensor(value_local):
                                data[name][name_local] = torch.cat((data[name][name_local], value_local), 0)
                    elif isinstance(value, list):
                        data[name].extend(value)

        return data
    def cat_tuples(self, tuple1, tuple2):
        list1 = list(tuple1)
        list2 = list(tuple2)
        list_all = list1.copy()
        list_all.extend(list2)
        list_all = list(set(list_all))
        if "" in list_all:
            list_all.remove("")
        list_all = tuple(list_all)
        return list_all
    def manual_zero_grad(self, model):
        if self.meta_param['flag_manual_zero_grad'] == 'delete':
            for name, param in model.named_parameters():  # parameter grad_zero
                if param.grad is not None:
                    param.grad = None
        elif self.meta_param['flag_manual_zero_grad'] == 'zero':
            for name, param in model.named_parameters():  # parameter grad_zero
                if param.grad is not None:
                    if torch.sum(param.grad) > 0:
                        param.grad.zero_()
        # return model
    def _detect_anomaly(self, losses, loss_dict):
        if not torch.isfinite(losses).all():
            raise FloatingPointError(
                "Loss became infinite or NaN at iteration={}!\nloss_dict = {}".format(
                    self.iter, loss_dict
                )
            )
    def _write_metrics(self, metrics_dict: dict):
        """
        Args:
            metrics_dict (dict): dict of scalar metrics
        """
        metrics_dict = {
            k: v.detach().cpu().item() if isinstance(v, torch.Tensor) else float(v)
            for k, v in metrics_dict.items()
        }
        # gather metrics among all workers for logging
        # This assumes we do DDP-style training, which is currently the only
        # supported method in fastreid.
        all_metrics_dict = comm.gather(metrics_dict)

        if comm.is_main_process():
            if "data_time" in all_metrics_dict[0]:
                # data_time among workers can have high variance. The actual latency
                # caused by data_time is the maximum among workers.
                data_time = np.max([x.pop("data_time") for x in all_metrics_dict])
                self.storage.put_scalar("data_time", data_time)

            # average the rest metrics
            metrics_dict = {
                k: np.mean([x[k] for x in all_metrics_dict]) for k in all_metrics_dict[0].keys()
            }
            total_losses_reduced = sum(loss for loss in metrics_dict.values())

            self.storage.put_scalar("total_loss", total_losses_reduced)
            if len(metrics_dict) > 1:
                self.storage.put_scalars(**metrics_dict)
