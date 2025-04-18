from typing import Optional, Callable, Tuple, Dict, List
from collections import namedtuple
import numpy as np
import torch

from ding.envs import BaseEnvManager
from ding.torch_utils import to_tensor, to_ndarray, to_item
from ding.utils import build_logger, EasyTimer, SERIAL_EVALUATOR_REGISTRY
from ding.utils import get_world_size, get_rank, broadcast_object_list
from .base_serial_evaluator import ISerialEvaluator, VectorEvalMonitor


@SERIAL_EVALUATOR_REGISTRY.register('interaction')
class InteractionSerialEvaluator(ISerialEvaluator):
    """
    Overview:
        Interaction serial evaluator class, policy interacts with env.
    Interfaces:
        __init__, reset, reset_policy, reset_env, close, should_eval, eval
    Property:
        env, policy
    """

    config = dict(
        # (int) Evaluate every "eval_freq" training iterations.
        eval_freq=1000,
        render=dict(
            # Tensorboard video render is disabled by default.
            render_freq=-1,
            mode='train_iter',
        ),
        # (str) File path for visualize environment information.
        figure_path=None,
    )

    def __init__(
            self,
            cfg: dict,
            env: BaseEnvManager = None,
            policy: namedtuple = None,
            tb_logger: 'SummaryWriter' = None,  # noqa
            exp_name: Optional[str] = 'default_experiment',
            instance_name: Optional[str] = 'evaluator',
    ) -> None:
        """
        Overview:
            Init method. Load config and use ``self._cfg`` setting to build common serial evaluator components, \
            e.g. logger helper, timer.
        Arguments:
            - cfg (:obj:`EasyDict`): Configuration EasyDict.
        """
        self._cfg = cfg
        self._exp_name = exp_name
        self._instance_name = instance_name

        # Logger (Monitor will be initialized in policy setter)
        # Only rank == 0 learner needs monitor and tb_logger, others only need text_logger to display terminal output.
        if get_rank() == 0:
            if tb_logger is not None:
                self._logger, _ = build_logger(
                    './{}/log/{}'.format(self._exp_name, self._instance_name), self._instance_name, need_tb=False
                )
                self._tb_logger = tb_logger
            else:
                self._logger, self._tb_logger = build_logger(
                    './{}/log/{}'.format(self._exp_name, self._instance_name), self._instance_name
                )
        else:
            self._logger, self._tb_logger = None, None  # for close elegantly
        self.reset(policy, env)

        self._timer = EasyTimer()
        self._default_n_episode = cfg.n_episode
        self._stop_value = cfg.stop_value
        # only one freq
        self._render = cfg.render
        assert self._render.mode in ('envstep', 'train_iter'), 'mode should be envstep or train_iter'

    def reset_env(self, _env: Optional[BaseEnvManager] = None) -> None:
        """
        Overview:
            Reset evaluator's environment. In some case, we need evaluator use the same policy in different \
                environments. We can use reset_env to reset the environment.
            If _env is None, reset the old environment.
            If _env is not None, replace the old environment in the evaluator with the \
                new passed in environment and launch.
        Arguments:
            - env (:obj:`Optional[BaseEnvManager]`): instance of the subclass of vectorized \
                env_manager(BaseEnvManager)
        """
        if _env is not None:
            self._env = _env
            self._env.launch()
            self._env_num = self._env.env_num
        else:
            self._env.reset()

    def reset_policy(self, _policy: Optional[namedtuple] = None) -> None:
        """
        Overview:
            Reset evaluator's policy. In some case, we need evaluator work in this same environment but use\
                different policy. We can use reset_policy to reset the policy.
            If _policy is None, reset the old policy.
            If _policy is not None, replace the old policy in the evaluator with the new passed in policy.
        Arguments:
            - policy (:obj:`Optional[namedtuple]`): the api namedtuple of eval_mode policy
        """
        assert hasattr(self, '_env'), "please set env first"
        if _policy is not None:
            self._policy = _policy
        self._policy_cfg = self._policy.get_attribute('cfg')
        self._policy.reset()

    def reset(self, _policy: Optional[namedtuple] = None, _env: Optional[BaseEnvManager] = None) -> None:
        """
        Overview:
            Reset evaluator's policy and environment. Use new policy and environment to collect data.
            If _env is None, reset the old environment.
            If _env is not None, replace the old environment in the evaluator with the new passed in \
                environment and launch.
            If _policy is None, reset the old policy.
            If _policy is not None, replace the old policy in the evaluator with the new passed in policy.
        Arguments:
            - policy (:obj:`Optional[namedtuple]`): the api namedtuple of eval_mode policy
            - env (:obj:`Optional[BaseEnvManager]`): instance of the subclass of vectorized \
                env_manager(BaseEnvManager)
        """
        if _env is not None:
            self.reset_env(_env)
        if _policy is not None:
            self.reset_policy(_policy)
        if self._policy_cfg.type == 'dreamer_command':
            self._states = None
            self._resets = np.array([False for i in range(self._env_num)])
        self._max_episode_return = float("-inf")
        self._last_eval_iter = -1
        self._end_flag = False
        self._last_render_iter = -1

    def close(self) -> None:
        """
        Overview:
            Close the evaluator. If end_flag is False, close the environment, flush the tb_logger\
                and close the tb_logger.
        """
        if self._end_flag:
            return
        self._end_flag = True
        self._env.close()
        if self._tb_logger:
            self._tb_logger.flush()
            self._tb_logger.close()

    def __del__(self):
        """
        Overview:
            Execute the close command and close the evaluator. __del__ is automatically called \
                to destroy the evaluator instance when the evaluator finishes its work
        """
        self.close()

    def should_eval(self, train_iter: int) -> bool:
        """
        Overview:
            Determine whether you need to start the evaluation mode, if the number of training has reached\
                the maximum number of times to start the evaluator, return True
        """
        if train_iter == self._last_eval_iter:
            return False
        if (train_iter - self._last_eval_iter) < self._cfg.eval_freq and train_iter != 0:
            return False
        self._last_eval_iter = train_iter
        return True

    def _should_render(self, envstep, train_iter):
        if self._render.render_freq == -1:
            return False
        iter = envstep if self._render.mode == 'envstep' else train_iter
        if (iter - self._last_render_iter) < self._render.render_freq:
            return False
        self._last_render_iter = iter
        return True

    def eval(
            self,
            save_ckpt_fn: Callable = None,
            train_iter: int = -1,
            envstep: int = -1,
            n_episode: Optional[int] = None,
            force_render: bool = False,
            policy_kwargs: Optional[Dict] = {},
    ) -> Tuple[bool, Dict[str, List]]:
        '''
        Overview:
            Evaluate policy and store the best policy based on whether it reaches the highest historical reward.
        Arguments:
            - save_ckpt_fn (:obj:`Callable`): Saving ckpt function, which will be triggered by getting the best reward.
            - train_iter (:obj:`int`): Current training iteration.
            - envstep (:obj:`int`): Current env interaction step.
            - n_episode (:obj:`int`): Number of evaluation episodes.
        Returns:
            - stop_flag (:obj:`bool`): Whether this training program can be ended.
            - episode_info (:obj:`Dict[str, List]`): Current evaluation episode information.
        '''
        # evaluator only work on rank0
        stop_flag = False
        episode_info = None  # Initialize to ensure it's defined in all ranks

        if get_rank() == 0:
            if n_episode is None:
                n_episode = self._default_n_episode
            assert n_episode is not None, "please indicate eval n_episode"
            envstep_count = 0
            info = {}
            eval_monitor = VectorEvalMonitor(self._env.env_num, n_episode)
            self._env.reset()
            self._policy.reset()

            # force_render overwrite frequency constraint
            render = force_render or self._should_render(envstep, train_iter)

            with self._timer:
                while not eval_monitor.is_finished():
                    obs = self._env.ready_obs
                    obs = to_tensor(obs, dtype=torch.float32)

                    # update videos
                    if render:
                        eval_monitor.update_video(self._env.ready_imgs)

                    if self._policy_cfg.type == 'dreamer_command':
                        policy_output = self._policy.forward(
                            obs, **policy_kwargs, reset=self._resets, state=self._states
                        )
                        #self._states = {env_id: output['state'] for env_id, output in policy_output.items()}
                        self._states = [output['state'] for output in policy_output.values()]
                    else:
                        policy_output = self._policy.forward(obs, **policy_kwargs)
                    actions = {i: a['action'] for i, a in policy_output.items()}
                    actions = to_ndarray(actions)
                    timesteps = self._env.step(actions)
                    timesteps = to_tensor(timesteps, dtype=torch.float32)
                    for env_id, t in timesteps.items():
                        if t.info.get('abnormal', False):
                            # If there is an abnormal timestep, reset all the related variables(including this env).
                            self._policy.reset([env_id])
                            continue
                        if self._policy_cfg.type == 'dreamer_command':
                            self._resets[env_id] = t.done
                        if t.done:
                            # Env reset is done by env_manager automatically.
                            if 'figure_path' in self._cfg and self._cfg.figure_path is not None:
                                self._env.enable_save_figure(env_id, self._cfg.figure_path)
                            self._policy.reset([env_id])
                            reward = t.info['eval_episode_return']
                            saved_info = {'eval_episode_return': t.info['eval_episode_return']}
                            if 'episode_info' in t.info:
                                saved_info.update(t.info['episode_info'])
                            eval_monitor.update_info(env_id, saved_info)
                            eval_monitor.update_reward(env_id, reward)
                            self._logger.info(
                                "[EVALUATOR]env {} finish episode, final reward: {:.4f}, current episode: {}".format(
                                    env_id, eval_monitor.get_latest_reward(env_id), eval_monitor.get_current_episode()
                                )
                            )
                        envstep_count += 1
            duration = self._timer.value
            episode_return = eval_monitor.get_episode_return()
            info = {
                'train_iter': train_iter,
                'ckpt_name': 'iteration_{}.pth.tar'.format(train_iter),
                'episode_count': n_episode,
                'envstep_count': envstep_count,
                'avg_envstep_per_episode': envstep_count / n_episode,
                'evaluate_time': duration,
                'avg_envstep_per_sec': envstep_count / duration,
                'avg_time_per_episode': n_episode / duration,
                'reward_mean': np.mean(episode_return),
                'reward_std': np.std(episode_return),
                'reward_max': np.max(episode_return),
                'reward_min': np.min(episode_return),
                # 'each_reward': episode_return,
            }
            episode_info = eval_monitor.get_episode_info()
            if episode_info is not None:
                info.update(episode_info)
            self._logger.info(self._logger.get_tabulate_vars_hor(info))
            # self._logger.info(self._logger.get_tabulate_vars(info))
            for k, v in info.items():
                if k in ['train_iter', 'ckpt_name', 'each_reward']:
                    continue
                if not np.isscalar(v):
                    continue
                self._tb_logger.add_scalar('{}_iter/'.format(self._instance_name) + k, v, train_iter)
                self._tb_logger.add_scalar('{}_step/'.format(self._instance_name) + k, v, envstep)

            if render:
                video_title = '{}_{}/'.format(self._instance_name, self._render.mode)
                videos = eval_monitor.get_video()
                render_iter = envstep if self._render.mode == 'envstep' else train_iter
                from ding.utils import fps
                self._tb_logger.add_video(video_title, videos, render_iter, fps(self._env))

            episode_return = np.mean(episode_return)
            if episode_return > self._max_episode_return:
                if save_ckpt_fn:
                    save_ckpt_fn('ckpt_best.pth.tar')
                self._max_episode_return = episode_return
            stop_flag = episode_return >= self._stop_value and train_iter > 0
            if stop_flag:
                self._logger.info(
                    "[DI-engine serial pipeline] " + "Current episode_return: {:.4f} is greater than stop_value: {}".
                    format(episode_return, self._stop_value) + ", so your RL agent is converged, you can refer to " +
                    "'log/evaluator/evaluator_logger.txt' for details."
                )

        if get_world_size() > 1:
            objects = [stop_flag, episode_info]
            broadcast_object_list(objects, src=0)
            stop_flag, episode_info = objects

        # Ensure episode_info is converted to the correct format
        episode_info = to_item(episode_info) if episode_info is not None else {}

        return stop_flag, episode_info
