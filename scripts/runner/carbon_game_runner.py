from typing import Callable, Dict, List, Optional, Tuple, Type, Union, Any, AnyStr
import numbers
import random
import copy
from collections import defaultdict, namedtuple
from easydict import EasyDict

import copy
import time

import numpy as np
import torch
import torch.nn.functional as F

from timer import timer

from utils.dictlist import DictList
from utils.utils import calculate_gard_norm
from scripts.runner.replay_buffer import ReplayBuffer
from algorithms.policy import Policy


class RingBuffer:
    """ class that implements a not-yet-full buffer """

    def __init__(self, max_size):
        self.max = max_size
        self.data = []

    class __Full:
        """ class that implements a full buffer """

        def append(self, x):
            """ Append an element overwriting the oldest one. """
            self.data[self.cur] = x
            self.cur = (self.cur + 1) % self.max

        def get(self):
            """ return list of elements in correct order """
            ret = self.data[self.cur:] + self.data[:self.cur]
            return ret

        def last(self):
            return self.data[self.cur - 1] if self.cur > 0 else self.data[-1]

        def reset(self):
            self.cur = 0
            self.data.clear()
            self.__class__ = RingBuffer

    def append(self, x):
        """append an element at the end of the buffer"""
        self.data.append(x)
        if len(self.data) == self.max:
            self.cur = 0
            # Permanently change self's class from non-full to full
            self.__class__ = self.__Full

    def get(self):
        """ Return a list of elements from the oldest to the newest. """
        return self.data

    def last(self):
        return self.data[-1]

    def reset(self):
        self.cur = 0
        self.data.clear()


class TrajectoryBuffer:
    def __init__(self, n_envs: int, episode_length: int):
        self.n_envs = n_envs
        self.episode_length = episode_length

        self.env_agent_ids = {id_: set() for id_ in range(self.n_envs)}
        self.data = dict()  # eg. key -> env -> agent -> [step1, step2, ...]

    def get_env_data(self, env_id: int, agent_first: bool = True, rollover_agent_ids=None) -> Dict[
        AnyStr, Dict[AnyStr, List[Any]]]:
        rollover_agent_ids = set() if rollover_agent_ids is None else set(rollover_agent_ids)

        return_value = {}

        for key, env_data in self.data.items():  # data: {key: env_id, value: {agent_id: agent's step values}}
            for agent_id, values in env_data[env_id].items():
                first_key, second_key = (agent_id, key) if agent_first else (key, agent_id)
                if first_key not in return_value:
                    return_value[first_key] = {}

                values = copy.deepcopy(values.get())
                return_value[first_key][second_key] = np.array(values, dtype=np.float32)

                if agent_id in rollover_agent_ids:
                    env_data[env_id][agent_id].reset()
                    env_data[env_id][agent_id].append(values[-1])

        self.env_agent_ids[env_id].clear()
        for env_data in self.data.values():
            env_data[env_id] = {agent_id: value for agent_id, value in env_data[env_id].items()
                                if agent_id in rollover_agent_ids}
            self.env_agent_ids[env_id] = set(env_data[env_id].keys())

        return return_value

    def append(self, key: str, env_id: int, agent_id: str, value: Any):
        if key not in self.data:
            self.data[key] = {env_id: {} for env_id in range(self.n_envs)}

        if agent_id not in self.data[key][env_id]:
            self.data[key][env_id][agent_id] = RingBuffer(self.episode_length + 1)

        # 添加数据
        self.data[key][env_id][agent_id].append(value)
        self.env_agent_ids[env_id].add(agent_id)

    def contains_agent(self, env_id, agent_id: str):
        return agent_id in self.env_agent_ids[env_id]

    def reset(self):
        self.data.clear()
        for values in self.env_agent_ids.values():
            values.clear()


class CarbonGameRunner:
    def __init__(self, cfg: EasyDict):
        self._cfg = cfg
        self.env = cfg.env
        self.episodes = cfg.main_config.runner.episodes
        self.episode_length = cfg.main_config.runner.episode_length
        self.n_threads = cfg.main_config.envs.n_threads
        self.training_times = cfg.main_config.runner.training_times
        self.gamma = cfg.main_config.runner.gamma
        self.gae_lambda = cfg.main_config.runner.gae_lambda
        self.clip_epsilon = cfg.main_config.runner.clip_epsilon
        self.entropy_coef = cfg.main_config.runner.entropy_coef
        self.value_loss_coef = cfg.main_config.runner.value_loss_coef
        self.actor_max_grad_norm = cfg.main_config.runner.actor_max_grad_norm
        self.critic_max_grad_norm = cfg.main_config.runner.critic_max_grad_norm

        self._my_env_output = None
        self.trajectory_buffer = TrajectoryBuffer(self.n_threads, self.episode_length)
        self._replay_buffer = ReplayBuffer(cfg.main_config.runner.replay_buffer)

        self.policy = Policy(cfg)

        # 下面为selfplay的相关参数
        self.selfplay = True
        self._opponent_env_output = None
        self.best_model = None
        self.best_model_filename = None

    def run(self):
        if self.selfplay:
            self._my_env_output, self._opponent_env_output = self.env.reset(self.selfplay)
        else:
            self._my_env_output = self.env.reset(self.selfplay)

        self.trajectory_buffer.reset()

        for episode in range(self.episodes):
            self.prep_rollout()

            collect_logs = []
            with timer() as t:
                for step in range(self.episode_length):
                    new_data, collect_log = self.collect(step)

                    if new_data:  # add to replay buffer
                        self._replay_buffer.append(new_data)
                        collect_logs.extend(collect_log)
            collect_logs = {key: [d[key] for d in collect_logs] for key in collect_logs[0]}
            collect_logs['collect_time'] = t.elapse
            print(collect_logs)

            should_train = True  # TODO
            if should_train:
                self.prep_training()

                train_logs = []
                with timer() as t:
                    for _ in range(self.training_times):
                        batch_size = 256
                        n_iters = int(np.ceil(len(self._replay_buffer) / batch_size))
                        # train_data = self._replay_buffer.sample_batch(batch_size)  # TODO
                        for i in range(n_iters):
                            start = i * batch_size
                            end = min(start + batch_size, len(self._replay_buffer))
                            train_data = self._replay_buffer.sample_batch_by_indices(np.arange(start, end))  # TODO
                            train_log = self.train(train_data)
                            train_logs.append(train_log)
                train_log = {key: np.mean([d[key] for d in train_logs]) for key in train_logs[0]}
                train_log['train_time'] = t.elapse
                print(train_log)

                self._replay_buffer.reset()

    def save_policy_to_trajectory_buffer(self, policy_output: Dict[int, Dict[AnyStr, EasyDict]]):
        for env_id in range(self.n_threads):  # for each env
            for agent_id, agent_value in policy_output[env_id].items():  # for each agent
                for key, value in agent_value.items():  # S(t), a(t), V(t)
                    self.trajectory_buffer.append(key, env_id, agent_id, value)

    def collect(self, step) -> Tuple[List[Dict[AnyStr, Dict[AnyStr, List[np.ndarray]]]], List[Dict[AnyStr, float]]]:
        my_policy_output = self.get_actions_and_values(self._my_env_output)  # 我方策略输出
        self.save_policy_to_trajectory_buffer(my_policy_output)

        opponent_policy_output = None  # 对手策略输出
        if self.selfplay:
            opponent_policy_output = self.get_actions_and_values(self._opponent_env_output)  # TODO: use best model
            self.save_policy_to_trajectory_buffer(opponent_policy_output)
        
        env_actions = []
        for env_id in range(self.n_threads):  # for each env
            action = {agent_id: agent_value.action.item()
                      for agent_id, agent_value in my_policy_output[env_id].items()}  # agent_id: command value

            if opponent_policy_output is not None:  # 自我对局
                opponent_action = {agent_id: agent_value.action.item()
                                   for agent_id, agent_value in opponent_policy_output[env_id].items()}
                action = [action, opponent_action]

            env_actions.append(action)

        # a(t) -> r(t), S(t+1), done(t+1)
        if self.selfplay:
            self._my_env_output, self._opponent_env_output = self.env.step(env_actions)
        else:
            self._my_env_output = self.env.step(env_actions)

        return_data, collect_log = [], []
        for env_id in range(self.n_threads):  # 遍历每个游戏环境,并收集trajectory
            a_env_output_next = self._my_env_output[env_id]

            env_reward = a_env_output_next.pop('env_reward')
            a_env_output_next = copy.deepcopy(a_env_output_next)

            # 因reset被移动到reserved_agent_id中,
            agent_ids_next = a_env_output_next.pop('reserved_agent_id') if 'reserved_agent_id' in a_env_output_next \
                else a_env_output_next.pop('agent_id')

            # 处理t时刻
            for i, agent_id in enumerate(agent_ids_next):
                if not self.trajectory_buffer.contains_agent(env_id, agent_id):
                    continue

                self.trajectory_buffer.append('done', env_id, agent_id,
                                              a_env_output_next['done'][i])  # done(t+1)
                self.trajectory_buffer.append('reward', env_id, agent_id,
                                              a_env_output_next['reward'][i])  # r(t)

            if self.selfplay:
                opponent_env_output_next = self._opponent_env_output[env_id]
                opponent_env_output_next = copy.deepcopy(opponent_env_output_next)
                opponent_agent_ids_next = opponent_env_output_next.pop('reserved_agent_id') if 'reserved_agent_id' in opponent_env_output_next \
                    else opponent_env_output_next.pop('agent_id')

                for i, agent_id in enumerate(opponent_agent_ids_next):
                    if not self.trajectory_buffer.contains_agent(env_id, agent_id):
                        continue

                    self.trajectory_buffer.append('done', env_id, agent_id,
                                                  opponent_env_output_next['done'][i])  # done(t+1)
                    self.trajectory_buffer.append('reward', env_id, agent_id,
                                                  opponent_env_output_next['reward'][i])  # r(t)

            if all(a_env_output_next['done']):  # 游戏结束(t=terminal),收集所有的transition序列,并返回
                transitions = defaultdict(dict)
                env_data = self.trajectory_buffer.get_env_data(env_id, agent_first=True,
                                                               rollover_agent_ids=None)
                assert all([v['done'][-1] == 1 for v in env_data.values()])
                for agent_id, traj in env_data.items():
                    # 添加到transition中
                    for key, value in traj.items():
                        transitions[agent_id][key] = np.array(value)
                    returns = self.compute_returns(traj, next_value=0)
                    transitions[agent_id].update(returns)
                return_data.append(transitions)

                collect_log.append(EasyDict({
                    "alive_agent_count": len(agent_ids_next),
                    "env_return": env_reward,
                }))

        return return_data, collect_log

    def get_actions_and_values(self, env_output):
        agent_ids, obs, available_actions = zip(*[(output['agent_id'],
                                                   output['obs'],
                                                   output['available_actions'])
                                                  for output in env_output])
        flatten_obs = [value for env_obs in obs for value in env_obs]
        flatten_obs_tensor = torch.from_numpy(np.stack(flatten_obs))
        flatten_available_actions = np.concatenate(available_actions)

        policy_output = self.policy.get_actions_values(flatten_obs_tensor, flatten_available_actions)

        flatten_action, flatten_log_prob, flatten_value = policy_output  # a(t), V(t)

        output = defaultdict(dict)
        c = 0
        for env_id, agent_ids_per_env in enumerate(agent_ids):
            for agent_id in agent_ids_per_env:
                output[env_id][agent_id] = EasyDict(dict(
                    obs=flatten_obs[c],
                    action=flatten_action[c],
                    log_prob=flatten_log_prob[c],
                    value=flatten_value[c],
                    available_actions=flatten_available_actions[c],
                ))
                c += 1
        return output

    def train(self, batch: EasyDict) -> EasyDict:
        log_prob, dist_entropy, value = self.policy.evaluate_actions(batch.obs, batch.action, batch.available_actions)

        # actor loss
        ratio = torch.exp(log_prob - batch.log_prob)
        advantage = batch.advantage
        surr1 = ratio * advantage
        surr2 = torch.clamp(ratio, 1.0 - self.clip_epsilon, 1.0 + self.clip_epsilon) * advantage
        policy_loss = -torch.min(surr1, surr2).mean()
        actor_loss = policy_loss - self.entropy_coef * dist_entropy

        # value loss
        experience_value = batch.value
        experience_return = batch.return_
        value = value.reshape_as(batch.value)
        value_clipped = experience_value + torch.clamp(value - experience_value, -self.clip_epsilon, self.clip_epsilon)
        value_loss = F.mse_loss(value, experience_return)
        value_clipped_loss = F.mse_loss(value_clipped, experience_return)
        value_loss = torch.max(value_loss, value_clipped_loss).mean()
        critic_loss = self.value_loss_coef * value_loss

        with torch.no_grad():
            log_ratio = log_prob - batch.log_prob
            approx_kl = torch.mean((torch.exp(log_ratio) - 1) - log_ratio).cpu().numpy()

        # train
        self.policy.actor_optimizer.zero_grad()
        actor_loss.backward()
        actor_grad_norm = calculate_gard_norm(self.policy.actor_model.parameters())
        if self.actor_max_grad_norm is not None and self.actor_max_grad_norm > 0:
            torch.nn.utils.clip_grad_norm_(self.policy.actor_model.parameters(), self.actor_max_grad_norm)
        self.policy.actor_optimizer.step()

        self.policy.critic_optimizer.zero_grad()
        critic_loss.backward()
        critic_grad_norm = calculate_gard_norm(self.policy.critic_model.parameters())
        if self.critic_max_grad_norm is not None and self.critic_max_grad_norm > 0:
            torch.nn.utils.clip_grad_norm_(self.policy.critic_model.parameters(), self.critic_max_grad_norm)
        self.policy.critic_optimizer.step()

        # 返回统计情况
        output = EasyDict({
            "entropy": dist_entropy.mean().item(),
            "policy_loss": policy_loss.item(),
            "actor_loss": actor_loss.item(),
            "actor_grad_norm": actor_grad_norm,
            "advantage": advantage.mean().item(),
            "approx_kl": approx_kl.item(),
            "value": value.mean().item(),
            "critic_loss": critic_loss.item(),
            "critic_grad_norm": critic_grad_norm,
            "ratio": ratio.mean().item(),
        })
        return output

    def compute_returns(self, trajectory: Dict[AnyStr, List[Any]], next_value=0, use_gae=True):
        """
        Compute returns and advantages either as discounted sum of rewards, or using GAE.
        :param trajectory: (dict) Agent trajectory data of full steps.
        :param next_value: (float) value predictions for the step after the last episode step.
        :param use_gae: (bool) Use use generalized advantage estimation or not (default True).
        """
        episode_len = len(trajectory['value'])
        gae = 0

        advantages = np.zeros(episode_len)
        returns = np.zeros(episode_len)
        for t in reversed(range(episode_len)):
            next_mask = int(1 - trajectory['done'][t])

            if use_gae:
                next_value = trajectory['value'][t + 1] if t < episode_len - 1 else next_value
                delta = trajectory['reward'][t] + self.gamma * next_value * next_mask - trajectory['value'][t]
                advantages[t] = gae = delta + self.gamma * self.gae_lambda * next_mask * gae
                returns[t] = gae + trajectory['value'][t]
            else:
                next_value = trajectory['reward'][t] + self.gamma * next_value * next_mask
                returns[t] = next_value
                advantages[t] = returns[t] - trajectory['value'][t]

        return {"advantage": advantages, "return_": returns}

    def prep_training(self):
        self.policy.actor_model.train()
        self.policy.critic_model.train()

    def prep_rollout(self):
        self.policy.actor_model.eval()
        self.policy.critic_model.eval()
