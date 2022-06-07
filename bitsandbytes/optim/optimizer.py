# Copyright (c) Facebook, Inc. and its affiliates. 
#   
# This source code is licensed under the MIT license found in the 
# LICENSE file in the root directory of this source tree.
import torch
import bitsandbytes.functional as F

from copy import deepcopy
from itertools import chain
from collections import defaultdict, abc as container_abcs

class MockArgs(object):
    def __init__(self, initial_data):
        for key in initial_data:
            setattr(self, key, initial_data[key])

optim2state = set(['adam', 'adamw', 'lamb'])
optim1state = set(['momentum', 'rmsprop', 'adagrad', 'lars'])

class GlobalOptimManager(object):
    _instance = None

    def __init__(self):
        raise RuntimeError('Call get_instance() instead')

    def initialize(self):
        self.pid2config = {}
        self.index2config = {}
        self.optimizer = None
        self.uses_config_override = False
        self.module_weight_config_triple = []

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls.__new__(cls)
            cls._instance.initialize()
        return cls._instance

    def register_module_override(self, module, param_name, config):
        self.module_weight_config_triple.append((module, param_name, config))



class BaseOptimizer8bit(torch.optim.Optimizer):

    def __init__(self, params, defaults, optim_bits=32, streaming=False):
        super(BaseOptimizer8bit, self).__init__(params, defaults)
        self.initialized = False
        self.name2qmap = {}
        self.streaming = streaming

        self.mng = GlobalOptimManager.get_instance()
        self.non_castable_tensor_keys = set(
                ['qmap1', 'qmap2',
                 'max1', 'max2',
                 'new_max1', 'new_max2',
                 'state1', 'state2',
                 'gnorm_vec', 'absmax1', 'absmax2',
                 'unorm_vec'])

        if optim_bits == 8: self.fill_qmap()

    def fill_qmap(self):
        self.name2qmap['dynamic'] = F.create_dynamic_map(signed=True)
        self.name2qmap['udynamic'] = F.create_dynamic_map(signed=False)

    def __setstate__(self, state):
        super(BaseOptimizer8bit, self).__setstate__(state)

    def get_quantization_map(self, name):
        if name == 'dynamic':
            return None, None
        else:
            raise NotImplementedError(f'The quantization technique is not supported for 8-bit optimizers')


    def load_state_dict(self, state_dict):
        r"""Loads the optimizer state.

        Args:
            state_dict (dict): optimizer state. Should be an object returned
                from a call to :meth:`state_dict`.
        """
        # deepcopy, to be consistent with module API
        state_dict = deepcopy(state_dict)
        # Validate the state_dict
        groups = self.param_groups
        saved_groups = state_dict['param_groups']

        if len(groups) != len(saved_groups):
            raise ValueError("loaded state dict has a different number of "
                             "parameter groups")
        param_lens = (len(g['params']) for g in groups)
        saved_lens = (len(g['params']) for g in saved_groups)
        if any(p_len != s_len for p_len, s_len in zip(param_lens, saved_lens)):
            raise ValueError("loaded state dict contains a parameter group "
                             "that doesn't match the size of optimizer's group")

        # Update the state
        id_map = {old_id: p for old_id, p in
                  zip(chain.from_iterable((g['params'] for g in saved_groups)),
                      chain.from_iterable((g['params'] for g in groups)))}

        def cast(param, value):
            r"""Make a deep copy of value, casting all tensors to device of param."""
            if isinstance(value, torch.Tensor):
                # Floating-point types are a bit special here. They are the only ones
                # that are assumed to always match the type of params.
                if param.is_floating_point() and value.dtype != torch.uint8:
                    value = value.to(param.dtype)
                return value
            elif isinstance(value, dict):
                for k, v in value.items():
                    if v is None: continue
                    if k in self.non_castable_tensor_keys:
                        value[k] = v.to(param.device)
                    else:
                        value[k] = cast(param, v)

                return value
            elif isinstance(value, container_abcs.Iterable):
                return type(value)(cast(param, v) for v in value)
            else:
                return value

        # Copy state assigned to params (and cast tensors to appropriate types).
        # State that is not assigned to params is copied as is (needed for
        # backward compatibility).
        state = defaultdict(dict)
        for k, v in state_dict['state'].items():
            if k in id_map:
                param = id_map[k]
                state[param] = cast(param, v)
            else:
                state[k] = v

        # Update parameter groups, setting their 'params' value
        def update_group(group, new_group):
            new_group['params'] = group['params']
            return new_group
        param_groups = [
            update_group(g, ng) for g, ng in zip(groups, saved_groups)]
        self.__setstate__({'state': state, 'param_groups': param_groups})

    def to_gpu(self):
        for gindex, group in enumerate(self.param_groups):
            for pindex, p in enumerate(group['params']):
                if p in self.state:
                    values = self.state[p]
                    for k, v in values.items():
                        if isinstance(v, torch.Tensor):
                            self.state[p][k] = v.to(p.device)

    def check_overrides(self):
        for module, attr, config in self.mng.module_weight_config_triple:
            pmodule = getattr(module, attr)
            assert pmodule is not None
            assert isinstance(pmodule, torch.Tensor) or isinstance(pmodule, torch.Parameter)
            found = False
            for gindex, group in enumerate(self.param_groups):
                if found: break
                for pindex, p in enumerate(group['params']):
                    if found: break
                    if id(p) == id(pmodule):
                        # found the matching parameter
                        # init override
                        self.mng.pid2config[id(p)] = config
                        self.mng.index2config[(gindex, pindex)] = self.mng.pid2config[id(p)]
                        found = True

    @torch.no_grad()
    def step(self, closure=None):
        """Performs a single optimization step.

        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        overflows = []

        if not self.initialized:
            self.check_overrides()
            self.to_gpu() # needed for fairseq pure fp16 training
            self.initialized = True


        managed_idx = []
        managed_buffers = []
        global_idx = 0
        if self.streaming:
            for gindex, group in enumerate(self.param_groups):
                for pindex, p in enumerate(group['params']):
                    state = self.state[p]
                    if 'state1' in state:
                        s = []
                        if 'state1' in state and getattr(state['state1'], 'is_managed', False): s.append(state['state1'])
                        if 'state2' in state and getattr(state['state2'], 'is_managed', False): s.append(state['state2'])
                        if len(s) > 0:
                            managed_idx.append(global_idx)
                            managed_buffers.append(s)
                    global_idx += 1

        global_idx = 0
        for gindex, group in enumerate(self.param_groups):
            for pindex, p in enumerate(group['params']):
                if p.grad is None:
                    continue
                state = self.state[p]
                if len(state) == 0:
                    self.init_state(group, p, gindex, pindex)

                if len(managed_buffers) > 0:
                    if managed_idx[0] == global_idx:
                        managed_idx.pop(0)
                        buffers = managed_buffers.pop(0)
                        for s in buffers:
                            F.prefetch_togpu(s, deviceid=torch.cuda.current_device())

                self.update_step(group, p, gindex, pindex)

                global_idx += 1

        return loss

    def get_config(self, gindex, pindex, group):
        config = {}
        config['betas'] = group['betas']
        config['eps'] = group['eps']
        config['weight_decay'] = group['weight_decay']
        config['lr'] = group['lr']
        config['optim_bits'] = self.args.optim_bits
        config['min_8bit_size'] = self.args.min_8bit_size
        config['percentile_clipping'] = getattr(self.args, 'percentile_clipping', 100)
        config['max_unorm'] = getattr(self.args, 'max_unorm', 0.0)
        config['skip_zeros'] = self.args.skip_zeros

        if (gindex, pindex) in self.mng.index2config:
            config.update(self.mng.index2config[(gindex, pindex)])
        return config

    def init_state(self, group, p, gindex, pindex):
        raise NotImplementedError(f'init_state method needs to be overidden')

    def update_step(self, group, p, gindex, pindex):
        raise NotImplementedError(f'The update_step method needs to be overidden')

class BNBOptimizer(BaseOptimizer8bit):
    def __init__(self, optimizer_name, params, lr=1e-3, betas=(0.9, 0.999), eps=1e-8,
            weight_decay=0.0, optim_bits=32, args=None,
            min_8bit_size=204800,
            skip_zeros=False, quant_maps_or_name='dynamic', streaming=False):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if isinstance(betas, str):
            # format: '(beta1, beta2)'
            betas = betas.replace('(', '').replace(')', '').strip().split(',')
            betas = [float(b) for b in betas]
        for i in range(len(betas)):
            if not 0.0 <= betas[i] < 1.0:
                raise ValueError(f"Invalid beta parameter at index {i}: {betas[i]}")
        if not 0.0 <= weight_decay:
            raise ValueError("Invalid weight_decay value: {}".format(weight_decay))
        defaults = dict(lr=lr, betas=betas, eps=eps,
                        weight_decay=weight_decay)
        super(BNBOptimizer, self).__init__(params, defaults, optim_bits, streaming)

        if args is None:
            args = {}
            args['optim_bits'] = optim_bits
            args['min_8bit_size'] = min_8bit_size
            args['skip_zeros'] = skip_zeros

            self.args = MockArgs(args)
        else:
            self.args = args

        self.optimizer_name = optimizer_name
        self.quant_maps_or_name = quant_maps_or_name

    @property
    def supports_flat_params(self):
        return True

    def get_state_buffer(self, p, dtype=torch.float32):
        if not self.streaming or len(p.shape) != 2 or p.numel() < 204800:
            return torch.zeros_like(p, dtype=dtype, device=p.device)
        else:
            buff = F.get_managed(*p.shape, dtype=dtype)
            F.fill(buff, 0)
            return buff

    @torch.no_grad()
    def init_state(self, group, p, gindex, pindex):
        config = self.get_config(gindex, pindex, group)

        if isinstance(self.quant_maps_or_name, str):
            qmap1, qmap2 = self.get_quantization_map(self.quant_maps_or_name)
        elif isinstance(self.quant_maps_or_name, list) or isinstance(self.quant_maps_or_name, tuple):
            qmap1, qmap2 = self.quant_maps_or_name
        else:
            raise NotImplementedError(f'Format for quantization map not supported: {type(self.quant_maps_or_name)}. Only types of str, tuple, or list supported!')

        n = p.numel()
        blocks = n//2048
        blocks += 1 if n % 2048 > 0 else 0

        if config['optim_bits'] == 32:
            dtype = torch.float32
        elif config['optim_bits'] == 8:
            dtype = torch.uint8
        else: raise NotImplementedError(f'Amount of optimizer bits not supported: {config["optim_bits"]}')

        state = self.state[p]
        state['step'] = 0

        if p.numel() < config['min_8bit_size']: dtype = torch.float32
        if dtype == torch.float32:
            state['state1'] = self.get_state_buffer(p)
            state['qmap1'] = None
            state['absmax1'] = None
        elif dtype == torch.uint8:
            state['state1'] = self.get_state_buffer(p, dtype=torch.uint8)
            state['qmap1'] = qmap1
            state['absmax1'] = torch.zeros((blocks,), dtype=torch.float32, device=p.device)

        if self.optimizer_name in optim2state:
            if dtype == torch.float32:
                state['state2'] = self.get_state_buffer(p)
                state['qmap2'] = None
                state['absmax2'] = None
            else:
                state['state2'] = self.get_state_buffer(p, dtype=torch.uint8)
                state['absmax2'] = torch.zeros((blocks,), dtype=torch.float32, device=p.device)
                state['qmap2'] = qmap2
        else:
            state['state2'] = None
            state['absmax2'] = None
            state['state2'] = None
            state['qmap2'] = None

        state['unorm_vec'] = None


    @torch.no_grad()
    def update_step(self, group, p, gindex, pindex):
        state = self.state[p]
        grad = p.grad

        config = self.get_config(gindex, pindex, group)

        state['step'] += 1
        step = state['step']

        F.bnb_optimizer_update(self.optimizer_name, grad, p, state, config)
