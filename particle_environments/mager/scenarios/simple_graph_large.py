"""Module for the Risk-Exploiting Graph Scenario (i.e. Simplified Circuit)
 
- Network of agents are to act as a connecition between origin and destination terminals of 
known (observed).
- This is a simplified, surrogate scenario for the more complex communication relay scenario 
since would be expected to produce similar physical behavior of the network without adding
the complexity of message passing to the action space 
- The aggregate reward is a function of the connection quality of the network between the terminals.
- The connection quality is modeled like an electrical circuit where resistance is a function
of distance between agents in the network. Connections can be serial and parallel.
Beyond a certain distance threshold, connections are not made between agents
- Landmarks can act to boost or degrade the connection quality of agents within a certain
proximity.
- Landmarks can also have a risk associated with them, i.e. probability of causing a nearby
agent to fail and eliminating if from the network.
- Agents actions are their movements
- Interesting behavior to be investigate: Discovered landmarks to either be approached or avoided
"""

import numpy as np
from bisect import bisect, insort
from shapely.geometry import LineString, Point
from multiagent.scenario import BaseScenario
from particle_environments.mager.world import SensingLimitedMortalAgent, HazardousWorld, RiskRewardLandmark
from particle_environments.common import is_collision, distance, delta_pos, nearest_point_on_line_segment_2d, check_2way_communicability
from particle_environments.common import RadialPolynomialRewardFunction2D as RadialReward
from particle_environments.common import RadialBernoulliRiskFunction2D as RadialRisk
from particle_environments.common import linear_index_to_lower_triangular, SimpleNetwork, truncate_or_pad
from rl_algorithms.scenariolearning import ScenarioHeuristicAgentTrainer


# Scenario Parameters
_MAX_CONNECTION_DISTANCE = 0.35
_MAX_OBSERVATION_DISTANCE = 1.0
_CONNECTION_REWARD = 1.0
_TERMINATION_REWARD = -0.0
_AGENT_SIZE = 0.01
_LANDMARK_SIZE = 0.025
_N_RADIAL_BINS = 4
_N_ANGULAR_BINS = 8
_N_OBSERVED_TERMINATIONS = 5
_N_TERMINALS = 2

_NON_TERMINAL_LANDMARKS = []
# _NON_TERMINAL_LANDMARKS.append(
#     RiskRewardLandmark( risk_fn=RadialRisk(0.1), reward_fn=RadialReward(0.1, 10.0)))

class Scenario(BaseScenario):
    # static class
    num_agents = 20

    def make_world(self):
        world = HazardousWorld()

        # set scenario-specific world parameters
        world.collaborative = True
        world.systemic_rewards = True
        world.identical_rewards = True
        world.dim_c = 0 # observation-based communication
        world.connection_reward = _CONNECTION_REWARD
        world.termination_reward = _TERMINATION_REWARD
        world.render_connections = True

        # add landmarks
        world.origin_terminal_landmark = RiskRewardLandmark( risk_fn=None, reward_fn=RadialReward(1.0, 10.0))
        world.destination_terminal_landmark = RiskRewardLandmark( risk_fn=None, reward_fn=RadialReward(1.0, 10.0))
        world.landmarks = [world.origin_terminal_landmark, world.destination_terminal_landmark]
        for lm in _NON_TERMINAL_LANDMARKS:
            lm.silent = True
            lm.deaf = True
            world.landmarks.append(lm)

        for i, landmark in enumerate(world.landmarks):
            landmark.name = 'landmark_%d' % i
            landmark.collide = False
            landmark.movable = False
            landmark.size = _LANDMARK_SIZE
            # properties for landmarks
            if isinstance(landmark, RiskRewardLandmark) and landmark.is_hazard:
                #TODO: make colors heatmap of risk probability over all bounds
                landmark.color = np.array([landmark.risk_fn.get_failure_probability(0,0) + .1, 0, 0])
            else:
                landmark.color = np.array([0.25, 0.25, 0.25])

        # make initial conditions
        self.reset_world(world)
        return world


    def reset_world(self, world):
        # random properties for agents
        # add agents
        world.agents = [SensingLimitedMortalAgent(_MAX_OBSERVATION_DISTANCE, _MAX_CONNECTION_DISTANCE) for i in range(self.num_agents)]
        for i, agent in enumerate(world.agents):
            agent.name = 'agent_%d' % i
            agent.collide = True
            agent.blind = False
            agent.silent = False
            agent.deaf = False
            agent.terminated = False
            agent.size = _AGENT_SIZE
            agent.state.p_pos = np.random.uniform(-1, +1, world.dim_p)
            agent.state.p_vel = np.zeros(world.dim_p)
            agent.state.c = np.zeros(world.dim_c)
            agent.color = np.array([0.35, 0.35, 0.85])
            agent.previous_observation = None

        for landmark in world.landmarks:
            # landmark.state.p_pos = np.random.uniform(-1, +1, world.dim_p)
            landmark.state.p_pos = np.zeros(world.dim_p)
            landmark.state.p_vel = np.zeros(world.dim_p)

        # randomize terminal locations, but regularize to ensure conistent distances
        origin_state, destination_state = self.spawn_terminals(world)
        world.origin_terminal_landmark.state.p_pos = origin_state
        world.destination_terminal_landmark.state.p_pos = destination_state


    def spawn_terminals(self, world):
        ''' create communication terminals at random positions but regularized distance
        Notes:
         - regularizing the distance between terminals is important to ensure consistency in 
         max rewards possible between different episodes
        '''

        # angle of line connecting terminals
        th = np.random.uniform(0, 2.0*np.pi)

        # half-distance between terminals
        # d = np.random.normal(2.0, 0.1)
        d = 2.0
        dx = d/2.0*np.cos(th)
        dy = d/2.0*np.sin(th)

        # center of line connecting terminals
        # xc = np.random.normal(0.0, 0.1)
        # yc = np.random.normal(0.0, 0.1)
        xc = yc = 0.0

        return  (np.array([xc-dx, yc-dy]), np.array([xc+dx, yc+dy]))

    def benchmark_data(self, agent, world):
        collisions = 0
        occupied_landmarks = 0
        min_dists = 0
        for l in world.landmarks:
            dists = [np.linalg.norm(a.state.p_pos - l.state.p_pos) for a in world.agents]
            min_dists += min(dists)
            if min(dists) < 0.1:
                occupied_landmarks += 1
        if agent.collide:
            for a in world.agents:
                if is_collision(a, agent):
                    collisions += 1
        return (self.reward(agent, world), collisions, min_dists, occupied_landmarks)

    def reward(self, agent, world, systemic_call=False):
        ''' individual or rewards per agent
        Args:
        Notes:
         - returns 1 if part of connection, 0 if not, and -1 if terminated
        '''
        
        if systemic_call:
            return self._systemic_reward(world)
        else:
            return 0.0

    def _systemic_reward(self, world):
        ''' reward for connectivity to each landmark
        Notes:
        '''
        assert world.identical_rewards
        comm_net = self._create_network(world)

        reward_n = [world.connection_reward*comm_net.breadth_first_connectivity_search(0,1)]*self.num_agents
        # node_count = 2
        # for i, a in enumerate(world.agents):
        #     if a.terminated:
        #         reward_n[i] = world.termination_reward
        #     else:
        #         # check ordering has stayed consistent in node list
        #         assert(a==comm_net.nodes[node_count])
        #         reward_n[i] = world.connection_reward*(
        #             comm_net.breadth_first_connectivity_search(node_count, 0) and
        #             comm_net.breadth_first_connectivity_search(node_count, 1)
        #             )
        #         node_count += 1

        return reward_n


    def _create_network(self, world):
        ''' Establish connectivity network at every time step
        '''

        # define nodes in simple connectivity network
        # by construction, node 0 is origin landmark, node 1 is destination landmark
        # terminated agents are not part of network
        nodes = [world.origin_terminal_landmark, world.destination_terminal_landmark]
        nodes.extend([a for a in world.agents if not a.terminated])
        n_nodes = len(nodes)
        comm_net = SimpleNetwork(nodes)

        # init list to hold direct communication distance values between agents
        # there is no direct communication between origin and destination
        n_pairs = int(n_nodes*(n_nodes+1)/2)
        
        # calculate direct communication resistance between agents
        for k in range(n_pairs):
            i,j = linear_index_to_lower_triangular(k)
            if i==1 and j==0: continue # enforce that origin & destination don't directly connect
            if check_2way_communicability(nodes[i], nodes[j]):
                comm_net.add_edge(i, j)

        # systemic reward is inverse of resistance (conductance)
        return comm_net


    def observation(self, agent, world):
        # get positions of all entities in this agent's reference frame

        # Observe communication terminals
        terminals = (world.origin_terminal_landmark.state.p_pos.tolist() + 
                    world.destination_terminal_landmark.state.p_pos.tolist())

        # Format agent histograms
        # bin_depth = _MAX_OBSERVATION_DISTANCE/float(_N_RADIAL_BINS)
        # radial_bins = np.linspace(bin_depth, _MAX_OBSERVATION_DISTANCE, num=_N_RADIAL_BINS)
        bin_depth = _MAX_OBSERVATION_DISTANCE/10.0
        radial_bins = np.logspace(np.log10(bin_depth), np.log10(_MAX_OBSERVATION_DISTANCE), num=_N_RADIAL_BINS)
        bin_angle = 2.0*np.pi/float(_N_ANGULAR_BINS)
        angular_bins = np.linspace(bin_angle/2.0, 2*np.pi - bin_angle/2.0, num=_N_ANGULAR_BINS)
        agent_histogram_2d = np.array([[0]*_N_ANGULAR_BINS]*_N_RADIAL_BINS)

        # establish observation of failures
        observed_terminations_2d = []
        observed_terminations_dists = []

        # count agents in each bin
        for a in world.agents:
            dist = distance(a, agent)

            # skip if agent is agent
            if a == agent:
                continue

            # record observed termination
            if a.terminated:
                insert_index = bisect(observed_terminations_dists, dist)
                observed_terminations_dists.insert(insert_index, dist)
                observed_terminations_2d.insert(insert_index, delta_pos(a, agent))
                continue

            # skip if outside of observation range
            if not agent.is_entity_observable(a):
                continue

            # find radial bin
            rad_bin = np.searchsorted(radial_bins, dist)

            # calculate angle
            dx, dy = delta_pos(a, agent)
            ang = np.arctan2(dy, dx)
            if ang < 0:
                ang += 2*np.pi

            # find angular bin
            ang_bin = np.searchsorted(angular_bins, ang)
            if ang_bin == _N_ANGULAR_BINS:
                ang_bin = 0

            # add count to histogram
            agent_histogram_2d[rad_bin][ang_bin] = agent_histogram_2d[rad_bin][ang_bin] + 1

        # flatten histogram to 1d list
        agent_histogram = [val for sublist in agent_histogram_2d for val in sublist]

        # flatten, truncate/pad observed terminations to fixed length
        observed_terminations = [val for sublist in observed_terminations_2d for val in sublist]
        observed_terminations = truncate_or_pad(observed_terminations, 2*_N_OBSERVED_TERMINATIONS)

        # package new observation
        new_obs = np.asarray([agent.terminated] + agent.state.p_vel.tolist() + agent.state.p_pos.tolist() + terminals + agent_histogram + observed_terminations)
        # if agent.terminated:
        #     # if agent is terminated, return all zeros for observation
        #     # TODO: make this more efficient. Right now it does a lot of unnecessary calcs which are all
        #     #   then set to zero. Done this way to ensure consistant array size
        #     new_obs = 0.0*new_obs

        # append previous observation for velocity estimation
        if agent.previous_observation is None:
            agent.previous_observation = 0.0*new_obs
        obs = np.append(new_obs, agent.previous_observation)
        agent.previous_observation = new_obs

        return obs


class ScenarioHeuristicComputer(ScenarioHeuristicAgentTrainer):
    ''' representation of an individual agent's embedded processor and memory tailor
    Notes:
     - This is meant to be used as a scenario-specific alternative to
     the more general purpose, scenario-agnostic "trainers". It can hold an agents model
     of the world (transition and reward functions), policy, and learning process, if any.
    '''
    def __init__(self, name, model, obs_shape_n, act_space_n, agent_index, args, **kwargs):
        ScenarioHeuristicAgentTrainer.__init__(self, name, model, obs_shape_n, act_space_n, agent_index, args)

        # record cumulative reward
        self.cumulative_reward = 0

        # behavior params
        # bp = dict()
        # bp['terminal_line_target_gain'] = 0.5
        # bp['terminal_line_target_exp'] = 1.0
        # bp['agent_proximity_gain'] = 0.025
        # bp['agent_velocity_gain'] = 0.01
        # bp['failure_avoidance_gain'] = 0.01
        # bp['random_action_x_bias'] = 0.0
        # bp['random_action_x_std'] = 0.05
        # bp['random_action_y_bias'] = 0.0
        # bp['random_action_y_std'] = 0.05
        # bp['terminal_line_tangent_gain'] = 0.1

        # behavior params
        bp_dist = self.get_initial_policy_distribution()
        bp = dict()
        for k in bp_dist:
            bp[k] = bp_dist[k][0]
        self.behavior_params = bp

        # observation range indices
        assert(_N_TERMINALS==2)
        oir = dict()
        oir['terminated'] = [0,0]
        oir['velocity'] = [1,2]
        oir['position'] = [3,4]
        oir['terminals'] = [5,8]
        oir['histogram'] = [9,8+_N_RADIAL_BINS*_N_ANGULAR_BINS]
        oir['failures'] = [oir['histogram'][1]+1, oir['histogram'][1]+2*_N_OBSERVED_TERMINATIONS]
        oir['previous_observation'] = [oir['failures'][1]+1, 2*oir['failures'][1]+1]
        self.observation_index_ranges = oir


    def action(self, obs):
        ''' maps observation array to action forces in x,y directions
        Notes:
         - Assumes observation array formated as:
            [0:2] = agent.state.p_vel.tolist() 
            [2:4] = agent.state.p_pos.tolist() 
            [4:8] = terminals
            [8:40] = agent_histogram + 
            failures)
        '''

        # rename for ease of use
        oir = self.observation_index_ranges
        fx = 0.0
        fy = 0.0

        # find closest point on line
        target_point = nearest_point_on_line_segment_2d(a=np.array(obs[4:6]), b=np.array(obs[6:8]), p=np.array(obs[2:4]))

        # force is proportional to distance to target
        dx_tar = target_point[0] - obs[oir['position'][0]]
        # fx += self.behavior_params['terminal_line_target_gain']*(
        #     dx_tar**self.behavior_params['terminal_line_target_exp'])
        fx += self.behavior_params['terminal_line_target_gain']*dx_tar
        dy_tar = target_point[1] - obs[oir['position'][1]]
        # fy += self.behavior_params['terminal_line_target_gain']*(
        #     dy_tar**self.behavior_params['terminal_line_target_exp'])
        fy += self.behavior_params['terminal_line_target_gain']*dy_tar

        # augment force with force perpendicular target in order to distribute along line
        f_tang = np.cross([dx_tar, dy_tar, 0.0], [0.0, 0.0, 1.0])
        fx += f_tang[0]*self.behavior_params['terminal_line_tangent_gain']
        fy += f_tang[1]*self.behavior_params['terminal_line_tangent_gain']

        # augment force based on proximity to other agents
        # nearest_radial_bins = obs[8:8+_N_ANGULAR_BINS]
        nearest_radial_bins = obs[oir['histogram'][0]:oir['histogram'][0]+_N_ANGULAR_BINS]
        nearest_radial_bins_prev = obs[oir['histogram'][0]+oir['previous_observation'][0] : oir['histogram'][0]+oir['previous_observation'][0]+_N_ANGULAR_BINS]
        for i, nrb in enumerate(nearest_radial_bins):
            angx, angy = self._get_angular_bin_vectors(i)
            fx += -angx*nrb*self.behavior_params['agent_proximity_gain']
            fy += -angy*nrb*self.behavior_params['agent_proximity_gain']
            dnrb = nrb - nearest_radial_bins_prev[i]
            fx += -angx*self.behavior_params['agent_velocity_gain']*max(dnrb, 0)
            fy += -angy*self.behavior_params['agent_velocity_gain']*max(dnrb, 0)

        # augment force based on proximity to observed terminations
        for i in range(oir['failures'][0], oir['failures'][1]+1, 2):
             fx += -obs[i]*self.behavior_params['failure_avoidance_gain']
             fy += -obs[i+1]*self.behavior_params['failure_avoidance_gain']

        # augment force based on random action
        # fx += np.random.normal(self.behavior_params['random_action_x_bias'], self.behavior_params['random_action_x_std'])
        # fy += np.random.normal(self.behavior_params['random_action_y_bias'], self.behavior_params['random_action_y_std'])

        # check action force is valid
        assert(not np.isnan(fx))
        assert(not np.isnan(fy))

        # Due to (bizarre) formulation of action input in MultiAgentEnv __init__ and _set_action
        # we need to create a 5-element vector where element [1] and [3] are the x,y componets of 
        # action force, respectivel
        act_force = np.zeros(5)
        act_force[1] = fx
        act_force[3] = fy

        return act_force

    def get_initial_policy_distribution(self):
        ''' method for "jumpstarting" monte carlo group distribution
        '''

        # (mean, std)
        bp_dist = dict()
        bp_dist['terminal_line_target_gain'] = (0.5, 1.0)
        # bp_dist['terminal_line_target_exp'] = (1.0, 1.0)
        bp_dist['agent_proximity_gain'] = (0.025, 0.1)
        bp_dist['agent_velocity_gain'] = (0.01, 0.1)
        bp_dist['failure_avoidance_gain'] = (0.01, 0.1)
        # bp_dist['random_action_x_bias'] = (0.0, 0.01)
        # bp_dist['random_action_x_std'] = (0.05, 0.1)
        # bp_dist['random_action_y_bias'] = (0.0, 0.01)
        # bp_dist['random_action_y_std'] = (0.05, 0.1)
        bp_dist['terminal_line_tangent_gain'] = (0.1, 0.1)

        # expdata.2018-10-25.mcgroup_simple_graph_large.0/policy
        # bp_dist = dict()
        # bp_dist['terminal_line_target_gain'] = (3.110812, 0.016378226)
        # # bp_dist['terminal_line_target_exp'] = (1.0, 1.0)
        # bp_dist['agent_proximity_gain'] = (0.2643773, 0.015730388)
        # bp_dist['agent_velocity_gain'] = (0.19096044, 0.007719264)
        # bp_dist['failure_avoidance_gain'] = (0.021809632, 0.022643076)
        # # bp_dist['random_action_x_bias'] = (0.0, 0.01)
        # # bp_dist['random_action_x_std'] = (0.05, 0.1)
        # # bp_dist['random_action_y_bias'] = (0.0, 0.01)
        # # bp_dist['random_action_y_std'] = (0.05, 0.1)
        # bp_dist['terminal_line_tangent_gain'] = (0.08197187, 0.012807179) 

        return bp_dist

    def experience(self, obs, act, rew, new_obs, done, terminal):
        ''' Monte Carlo learning only record cumulative reward
        '''
        # record cumulative reward
        self.cumulative_reward += rew


    def preupdate(self):
        '''unused function handle compatibility with train.py
        '''
        pass

    def update(self, agents, t):
        '''unused function handle compatibility with train.py
        '''
        pass

    def group_policy_update(self, group_policy):
        '''update behavior parameters based on group policy
        '''
        for k in group_policy:
            self.behavior_params[k] = group_policy[k]['clambda']

        # reset cumulative reward
        self.cumulative_reward = 0

    def _get_angular_bin_vectors(self, bin_num):
        ''' angle to centerline of angular bins in agent histogram
        Notes:
         - assumes 8 bins with centerlines at 0, 45, 90, etc
        '''
        assert(_N_ANGULAR_BINS==8)
        angle = bin_num*np.pi/4
        return np.cos(angle), np.sin(angle)
