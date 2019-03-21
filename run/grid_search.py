import time
from datetime import datetime
from copy import deepcopy
from multiprocessing import Process

from utility import yaml_op, utils


class GridSearch:
    def __init__(self, args_file, train_func, render=False, n_trials=1):
        args = yaml_op.load_args(args_file)
        self.env_args = args['env']
        self.agent_args = args['agent']
        self.buffer_args = args['buffer']
        self.train_func = train_func
        self.render = render
        self.n_trials = n_trials

        # add date to root directory
        now = datetime.now()
        for root_dir in ['model_root_dir', 'log_root_dir']:
            self.agent_args[root_dir] = f'data/{now.month:02d}{now.day:02d}/' + self.agent_args[root_dir]

        self.agent_args['model_dir'] = f"{self.agent_args['algorithm']}-{self.agent_args['model_dir']}"

        self.processes = []

    def __call__(self, **kwargs):
        if kwargs == {} and self.n_trials == 1:
            # if no argument is passed in, run the default setting
            self.train_func(self.env_args, self.agent_args, self.buffer_args, self.render)
            
        # do grid search
        now = datetime.now()
        self.agent_args['model_name'] = f'{now.month:02d}{now.day:02d}'
        self._change_args(**kwargs)
        [p.join() for p in self.processes]

    def _change_args(self, **kwargs):
        if kwargs == {}:
            # basic case
            old_model_name = self.agent_args['model_name']
            for i in range(self.n_trials):
                if self.n_trials > 1:
                    self.agent_args['model_name'] += f'/trial{i}'
                # arguments should be deep copied here, 
                # otherwise args will be reset if sub-process runs after
                p = Process(target=self.train_func,
                            args=(deepcopy(self.env_args), deepcopy(self.agent_args), 
                                  deepcopy(self.buffer_args), self.render))
                self.agent_args['model_name'] = old_model_name
                p.start()
                time.sleep(1)   # help make sure sub-processs start in order
                self.processes.append(p)
        else:
            # recursive case
            kwargs_copy = deepcopy(kwargs)
            key, value = self._popitem(kwargs_copy)
            valid_args = []
            for args in [self.agent_args, self.buffer_args, self.env_args]:
                valid_arg = args if key in args else False
                if valid_arg != False:
                    valid_args.append(valid_arg)
                    break
            # print(utils.colorize(f'kwargs after poping: {kwargs}', 'red'))
            err_msg = lambda k, v: utils.colorize(f'Invalid Argument: {k}={v}', 'red')
            assert valid_arg != False, err_msg(key, value)
            if isinstance(value, dict) and len(value) != 0:
                # For simplicity, we do not further consider the case when value is a dict of dicts here
                k, v = self._popitem(value)
                assert k in valid_arg[key], err_msg(k, v)
                if len(value) != 0:
                    # if there is still something left in value, put value back into kwargs
                    kwargs_copy[key] = value
                self._safe_call(f'-{key}', lambda: self._recursive_trial(valid_arg[key], k, v, kwargs_copy))
            else:
                self._recursive_trial(valid_arg, key, value, kwargs_copy)

    # helper functions for self._change_args
    def _popitem(self, kwargs):
        assert isinstance(kwargs, dict)
        while len(kwargs) != 0:
            k, v = kwargs.popitem()
            assert isinstance(v, dict) or isinstance(v, list)
            if len(v) != 0:
                break
        return k, v

    def _recursive_trial(self, arg, key, value, kwargs):
        assert isinstance(value, list), utils.colorize(f'Expect value of type list, not {type(value)}', 'red')
        for v in value:
            arg[key] = v
            self._safe_call(f'-{key}={v}', lambda: self._change_args(**kwargs))

    def _safe_call(self, append_name, func):
        old_model_name = self.agent_args['model_name']
        self.agent_args['model_name'] += append_name
        func()
        self.agent_args['model_name'] = old_model_name