from collections import namedtuple
from typing import Any, Callable, Dict, List, Tuple
from pathos.pools import ThreadPool as TPool
from copy import deepcopy
from fn.op import foldr, call

from cadCAD.engine.utils import engine_exception
from cadCAD.utils import flatten, UDC_Wrapper

id_exception: Callable = engine_exception(KeyError, KeyError, None)


class Executor:
    def __init__(
        self,
        policy_ops: List[Callable],
        policy_update_exception: Callable = id_exception,
        state_update_exception: Callable = id_exception
    ) -> None:

        # behavior_ops
        self.policy_ops = policy_ops
        self.state_update_exception = state_update_exception
        self.policy_update_exception = policy_update_exception
        # behavior_update_exception

    # get_behavior_input # sL: State Window
    def get_policy_input(
                self,
                var_dict: Dict[str, List[Any]],
                sub_step: int,
                sL: List[Dict[str, Any]],
                s: Dict[str, Any],
                funcs: List[Callable]
            ) -> Dict[str, Any]:

        ops = self.policy_ops[::-1]

        def get_col_results(var_dict, sub_step, sL, s, funcs):
            return list(map(lambda f: f(var_dict, sub_step, sL, s), funcs))

        return foldr(call, get_col_results(var_dict, sub_step, sL, s, funcs))(ops)

    def apply_env_proc(
                self,
                env_processes: Dict[str, Callable],
                state_dict: Dict[str, Any],
                sub_step: int
            ) -> None:
        for state in state_dict.keys():
            if state in list(env_processes.keys()):
                env_state: Callable = env_processes[state]
                if (env_state.__name__ == '_curried') or (env_state.__name__ == 'proc_trigger'):
                    state_dict[state] = env_state(sub_step)(state_dict[state])
                else:
                    state_dict[state] = env_state(state_dict[state])

    # mech_step
    def partial_state_update(
                self,
                var_dict: Dict[str, List[Any]],
                sub_step: int,
                sL: Any,
                state_funcs: List[Callable],
                policy_funcs: List[Callable],
                env_processes: Dict[str, Callable],
                time_step: int,
                run: int
            ) -> List[Dict[str, Any]]:

        last_in_obj: Dict[str, Any] = deepcopy(sL[-1])
        udc = var_dict[0]['udc']
        # last_in_obj: Dict[str, Any] = sL[-1]

        _input: Dict[str, Any] = self.policy_update_exception(self.get_policy_input(var_dict, sub_step, sL, last_in_obj, policy_funcs))

        # ToDo: add env_proc generator to `last_in_copy` iterator as wrapper function
        # ToDo: Can be multithreaded ??

        def generate_record(state_funcs, alt_udc_dict):
            for k, v in last_in_obj.items():
                if isinstance(v, dict) and hasattr(v, 'class_id'):
                    del last_in_obj[k]

            # def HydraObj(_g, step, sL, s, _input):
            #     y = 'hydra_obj'
            #     # x = s['hydra_obj']
            #     x = namedtuple("Hydra", s['hydra_members'].keys())(*s['hydra_members'].values())
            #     return (y, x)

            new_last_in_obj = dict(list(last_in_obj.items()) + list(alt_udc_dict.items()))
            # for f in state_funcs + [HydraObj]:
            for f in state_funcs:
                # ToDo: Create Named Tuple Here
                y, x = f(var_dict, sub_step, sL, new_last_in_obj, _input)
                # if isinstance(x, dict) and x['hydra_type'] == Dict and 'class_id' in x.keys():
                #     x = namedtuple("Hydra", x.keys())(*x.values())
                yield self.state_update_exception((y, x))


        udc_dict = {
            k: UDC_Wrapper(
                v['current'],
                udc(**v['current'].__dict__),
                current_functions=['update']
            ).get_hybrid_members()
            for k, v in last_in_obj.items() if isinstance(v, dict) and 'current' in v.keys()
        }
        last_in_copy: Dict[str, Any] = dict(generate_record(state_funcs, udc_dict))
        del udc_dict

        for k in last_in_obj:
            if k not in last_in_copy:
                last_in_copy[k] = last_in_obj[k]

        del last_in_obj

        self.apply_env_proc(env_processes, last_in_copy, last_in_copy['timestep'])

        # ToDo: make 'substep' & 'timestep' reserve fields
        last_in_copy['substep'], last_in_copy['timestep'], last_in_copy['run'] = sub_step, time_step, run

        sL.append(last_in_copy)
        del last_in_copy

        return sL

    # mech_pipeline - state_update_block
    def state_update_pipeline(
                self,
                var_dict: Dict[str, List[Any]],
                states_list: List[Dict[str, Any]],
                configs: List[Tuple[List[Callable], List[Callable]]],
                env_processes: Dict[str, Callable],
                time_step: int,
                run: int
            ) -> List[Dict[str, Any]]:

        sub_step = 0
        states_list_copy: List[Dict[str, Any]] = deepcopy(states_list)

        genesis_states: Dict[str, Any] = states_list_copy[-1]
        del states_list_copy
        genesis_states['substep'], genesis_states['timestep'] = sub_step, time_step
        states_list: List[Dict[str, Any]] = [genesis_states]

        sub_step += 1
        for config in configs:
            s_conf, p_conf = config[0], config[1]
            states_list: List[Dict[str, Any]] = self.partial_state_update(
                var_dict, sub_step, states_list, s_conf, p_conf, env_processes, time_step, run
            )

            sub_step += 1

        time_step += 1

        return states_list

    # state_update_pipeline
    def run_pipeline(
                self,
                var_dict: Dict[str, List[Any]],
                states_list: List[Dict[str, Any]],
                configs: List[Tuple[List[Callable], List[Callable]]],
                env_processes: Dict[str, Callable],
                time_seq: range,
                run: int
            ) -> List[List[Dict[str, Any]]]:

        time_seq: List[int] = [x + 1 for x in time_seq]
        simulation_list: List[List[Dict[str, Any]]] = [states_list]
        for time_step in time_seq:
            pipe_run: List[Dict[str, Any]] = self.state_update_pipeline(
                var_dict, simulation_list[-1], configs, env_processes, time_step, run
            )
            _, *pipe_run = pipe_run
            simulation_list.append(pipe_run)

        return simulation_list

    def simulation(
            self,
            var_dict: Dict[str, List[Any]],
            states_list: List[Dict[str, Any]],
            configs: List[Tuple[List[Callable], List[Callable]]],
            env_processes: Dict[str, Callable],
            time_seq: range,
            runs: int
        ) -> List[List[Dict[str, Any]]]:

        def execute_run(var_dict, states_list, configs, env_processes, time_seq, run) -> List[Dict[str, Any]]:
            run += 1
            states_list_copy: List[Dict[str, Any]] = deepcopy(states_list)

            head, *tail = self.run_pipeline(var_dict, states_list_copy, configs, env_processes, time_seq, run)
            del states_list_copy

            genesis: Dict[str, Any] = head.pop()
            genesis['substep'], genesis['timestep'], genesis['run'] = 0, 0, run
            first_timestep_per_run: List[Dict[str, Any]] = [genesis] + tail.pop(0)
            return [first_timestep_per_run] + tail

        pipe_run: List[List[Dict[str, Any]]] = flatten(
            TPool().map(
                lambda run: execute_run(var_dict, states_list, configs, env_processes, time_seq, run),
                list(range(runs))
            )
        )

        return pipe_run