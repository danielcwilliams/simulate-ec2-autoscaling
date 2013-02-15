#!/usr/bin/env python
#
# Copyright (c) 2013 Daniel Williams.
# 
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
# 
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#

"""%prog [options] service-distribution-pathname

Discrete event simulation program to model the behavior of Amazon Web
Services EC2 Autoscaling for a job queue.

The required pathname argument is a file that contains a sampling, one
per line, of the service time (in seconds) for jobs when run on an
m1.small instance. The input sequence of jobs will have a distribution
of values that are randomly chosen (with replacement) from the values
you specify in the file.

N.B. - the COST_MODEL data structure should be modified to reflect the
actual runtime performance, for your computations, of the various
instances you are interested in simulating, relative to an
m1.small. The values there now are just the EC2 Compute Unit measures
of compute capacity, something that is unlikely to be correct for your
computations."""

import optparse
import itertools
import random
import sys
import math

from SimPy.Simulation import Process, Monitor, initialize, hold, passivate, activate, reactivate, simulate, now
from SimPy.SimPlot import SimPlot, Frame, TOP, BOTH, YES

#
# cost of the AWS CloudWatch service (needed for AutoScaling) in
# dollars per server/hour
#
CLOUD_WATCH_COST = 0.00486

#
# tuple is: (price/hour, speed relative to an m1.small)
#
COST_MODEL = {'m1.small': (0.06 + CLOUD_WATCH_COST, 1),
              'm1.medium': (0.12 + CLOUD_WATCH_COST, 2),
              'm1.large': (0.24 + CLOUD_WATCH_COST, 4),
              'c1.medium': (0.14 + CLOUD_WATCH_COST, 5),
              'm2.xlarge': (0.41 + CLOUD_WATCH_COST, 6.5),
              'm1.xlarge': (0.48 + CLOUD_WATCH_COST, 8),
              'm3.xlarge': (0.50 + CLOUD_WATCH_COST, 13),
              'm2.2xlarge': (0.82 + CLOUD_WATCH_COST, 13),
              'c1.xlarge': (0.58 + CLOUD_WATCH_COST, 20),
              'm3.2xlarge': (1.00 + CLOUD_WATCH_COST, 26),
              'm2.4xlarge': (1.64 + CLOUD_WATCH_COST, 26),
              'cc1.4xlarge': (1.30 + CLOUD_WATCH_COST, 33.5),
              'cg1.4xlarge': (2.10 + CLOUD_WATCH_COST, 33.5),
              'hi1.4xlarge': (3.10 + CLOUD_WATCH_COST, 35),
              'hs1.8xlarge': (4.60 + CLOUD_WATCH_COST, 35),
              'cc2.8xlarge': (2.40 + CLOUD_WATCH_COST, 88),
              'cr1.8xlarge': (3.50 + CLOUD_WATCH_COST, 88)}

#
# real servers (especially with multiple processors) are almost never
# "pegged" even when fully loaded because there is always some blocking
# for I/O of some sort going on. Approximate that with this factor.
#
BUSY_FACTOR = 0.92


class Job(object):
    """A request for work to be performed by a Server"""
    arrivals = 0

    def __init__(self, name, normalized_service_time):
        self.name = name
        self.normalized_service_time = normalized_service_time
        self.arrival_time = now()
        Job.arrivals += 1


class Sources(Process):
    """Sources generate Jobs with random arrivals"""

    @staticmethod
    def sinusoidal_rate_modifier(rate, t, period, amplitude, phase, offset):
        """Allow rate to vary sinusoidally as a function of time."""
        newrate = (math.sin(t * 2.0 * math.pi / period + 2.0 * math.pi * phase) * amplitude + offset) * rate
        EPSILON = 1e-6
        return newrate if newrate >= EPSILON else EPSILON

    @staticmethod
    def profile_rate_modifier(t, profile):
        """Allow rate to vary as a function of time according to a piece-wise profile."""

        def linear(x, x0, x1, y0, y1):
            """Do linear interpolation given two data points."""
            a = (float(y0) - float(y1)) / (float(x0) - float(x1))
            b = float(y0) - a * float(x0)
            return a * x + b

        rate = 0
        if t >= profile[0][0] and t <= profile[-1][0]:
            for index, item in reversed(list(enumerate(profile))):
                if t >= item[0]:
                    rate = linear(t, item[0], profile[index + 1][0], item[1], profile[index + 1][1])
                    break
        EPSILON = 1e-2
        return max(rate, EPSILON)

    @staticmethod
    def random_stream_from_data(pathname):
        """Read a file of samples (one per line) and return an
        endless randomized stream of the contents."""
        with open(pathname, 'r') as f:
            data = [float(x) for x in f]
        while True:
            yield random.choice(data)

    def execute(self, arrival_rate, pathname, delay, period, amplitude, phase, offset, profile, rate):
        service_time_distribution = Sources.random_stream_from_data(pathname)
        yield hold, self, delay
        if profile:
            for i in itertools.count():
                yield hold, self, random.expovariate(Sources.profile_rate_modifier(now(), profile))
                j = Job("Job%d" % i, service_time_distribution.next())
                Server.queue.append(j)
                if Server.idle:
                    reactivate(random.choice(Server.idle))
        else:
            for i in itertools.count():
                yield hold, self, random.expovariate(Sources.sinusoidal_rate_modifier(arrival_rate, now(), period, amplitude, phase, offset))
                j = Job("Job%d" % i, service_time_distribution.next())
                Server.queue.append(j)
                if Server.idle:
                    reactivate(random.choice(Server.idle))


class Server(Process):
    busy = []                           # servers that are busy
    idle = []                           # servers that are idle
    queue = []                          # job queue for all servers
    server_time_used = 0.0
    server_time_wasted = 0.0
    jobs_processed = 0
    jobs_timed_out = 0
    time_in_system = Monitor('time in system')
    total_cost = 0.0
    total_capacity = Monitor('total capacity')
    counter = itertools.count()

    def __init__(self, max_wait, instance_type='m1.small', latency=0):
        self.terminated = False
        name = 'Server%d' % Server.counter.next()
        Process.__init__(self, name)
        self.max_wait = max_wait
        self.instance_type = instance_type
        self.latency = latency
        self.service_time_factor = 1.0 / COST_MODEL[self.instance_type][1]
        self.start_time = now()
        sys.stdout.write('+')
        sys.stdout.flush()

    @staticmethod
    def total():
        """Sum up costs of currently running servers and add them to
        running total. Do not round up like when computing
        termination costs."""
        for s in Server.busy + Server.idle:
            Server.total_cost += s.cost()

    def cost(self):
        return ((now() - self.start_time) / 3600.0) * COST_MODEL[self.instance_type][0]

    def cost_rounded_up(self):
        return math.ceil((now() - self.start_time) / 3600.0) * COST_MODEL[self.instance_type][0]

    @staticmethod
    def terminate():
        """Bring a (carefully chosen) server offline permanently"""

        #
        # http://aws.amazon.com/ec2/faqs/?preview=true#How_does_Auto_Scaling_decide_which_Amazon_EC2_instance
        #
        # Q: How does Auto Scaling decide which Amazon EC2 instance in
        # the Auto Scaling Group to terminate when the scaling
        # condition is met?
        #
        # A: When selecting an instance to terminate when a scaling
        # condition is met it will next prioritize the instance that
        # has been running for the longest portion of a billing hour
        # (without going over).
        #
        servers = Server.idle + Server.busy
        if servers:
            s = sorted([(x.cost_rounded_up() - x.cost(), x) for x in servers])[0][1]
        else:
            raise Exception('no servers to terminate')

        for l in (Server.busy, Server.idle):
            if s in l:
                l.remove(s)
                break
        else:
            raise Exception('server to terminate cannot be found')

        Server.total_capacity.observe(len(Server.busy) + len(Server.idle))
        Server.total_cost += s.cost_rounded_up()
        sys.stdout.write('-')
        sys.stdout.flush()
        s.terminated = True
        del s

    def execute(self):
        yield hold, self, self.latency
        if self.terminated:
            return
        Server.idle.append(self)
        Server.total_capacity.observe(len(Server.busy) + len(Server.idle))
        while True:
            #
            # sleep until we are awakened
            #
            yield passivate, self
            if self.terminated:
                return
            Server.idle.remove(self)
            Server.busy.append(self)
            #
            # process all jobs in the queue
            #
            while Server.queue:
                job = Server.queue.pop(0)

                #
                #                      max_wait
                # +----------------------------------------------------+
                # |                                                    |
                # arrival_time       start_time                        deadline
                # |------------------|-------------------|-------------|------------> T
                #                    |                   |                  |
                #                    +-------------------+                  |
                #                    |    service_time                      |
                #                    |                                      |
                #                    +--------------------------------------+
                #                                   service_time
                #
                service_time = job.normalized_service_time * self.service_time_factor
                deadline = job.arrival_time + self.max_wait
                start_time = now()
                if start_time + service_time < deadline:
                    # no timeout
                    yield hold, self, service_time
                    if self.terminated:
                        return
                    Server.time_in_system.observe(now() - job.arrival_time)
                    Server.jobs_processed += 1
                    Server.server_time_used += service_time
                elif start_time >= deadline:
                    # timeout before processing
                    Server.jobs_timed_out += 1
                else:
                    # timeout while processing
                    yield hold, self, deadline - start_time
                    if self.terminated:
                        return
                    Server.jobs_timed_out += 1
                    Server.server_time_wasted += (deadline - start_time)
                    Server.server_time_used += (deadline - start_time)
            #
            # go back to sleep
            #
            Server.busy.remove(self)
            Server.idle.append(self)


class Watcher(Process):
    """Implements AutoScaling"""
    cpu_utilization = Monitor('cpu utilization')
    arrivals_mon = Monitor('arrivals')

    def __init__(self, max_wait, instance_type, latency, period, breach_duration, min_size, max_size, cooldown, lower_threshold, lower_breach_scale_increment, upper_threshold, upper_breach_scale_increment):
        Process.__init__(self, 'Watcher')
        self.max_wait = max_wait
        self.instance_type = instance_type
        self.latency = latency
        #
        # aws autoscale settings
        #
        self.period = period
        self.breach_duration = breach_duration
        self.min_size = min_size
        self.max_size = max_size
        self.cooldown = cooldown
        self.lower_threshold = lower_threshold
        self.lower_breach_scale_increment = lower_breach_scale_increment
        self.upper_threshold = upper_threshold
        self.upper_breach_scale_increment = upper_breach_scale_increment

    def execute(self):
        checks = 0
        measures = []
        sample_period = 6
        last_scaling_activity = -1e6
        old_arrivals = 0

        while True:
            #
            # update cpu utilization across all servers
            #
            busy = len(Server.busy)
            both = busy + len(Server.idle)
            Watcher.cpu_utilization.observe(100.0 * float(BUSY_FACTOR * busy) / float(BUSY_FACTOR * busy + len(Server.idle)) if both else 0.0)

            #
            # do autoscaling checks every 60 seconds
            #
            checks += 1
            if checks % 10 == 0:
                #
                # compute the cpu utilization metric over the desired period
                #
                samples = self.period / sample_period
                utilization = sum(Watcher.cpu_utilization.yseries()[-samples:]) / samples
                measures.append(utilization)
                if len(measures) >= (self.breach_duration // self.period):
                    if len(measures) > (self.breach_duration // self.period):
                        measures.pop(0)
                    if now() - last_scaling_activity > self.cooldown:
                        new_server_target = both
                        if all([x > self.upper_threshold for x in measures]):
                            new_server_target = both + self.upper_breach_scale_increment
                            new_server_target = min(new_server_target, self.max_size)
                        elif all([x < self.lower_threshold for x in measures]):
                            new_server_target = both + self.lower_breach_scale_increment
                            new_server_target = max(new_server_target, self.min_size)
                        if new_server_target < both:
                            # scale down
                            for _ in range(both - new_server_target):
                                Server.terminate()
                            last_scaling_activity = now()
                        elif new_server_target > both:
                            # scale up
                            for _ in range(new_server_target - both):
                                s = Server(self.max_wait, self.instance_type, self.latency)
                                activate(s, s.execute())
                            last_scaling_activity = now()
                #
                # unrelated: also keep track of arrival rate
                #
                Watcher.arrivals_mon.observe(Job.arrivals - old_arrivals)
                old_arrivals = Job.arrivals

            yield hold, self, sample_period


if __name__ == '__main__':
    #
    # process command line arguments
    #
    parser = optparse.OptionParser(__doc__)
    parser.add_option('', '--seed', dest='seed', action='store', type='int', default=0, help='PRNG seed (default: %default means use an OS source)')
    parser.add_option('', '--capacity', dest='capacity', action='store', type='int', default=1, help='Set initial number of servers (default: %default)')
    parser.add_option('', '--trace', dest='trace', action='store_true', help='Trace simulation activity (very detailed)')
    parser.add_option('', '--plot', dest='plot', action='store_true', help='Plot capacity and load vs. time')
    parser.add_option('', '--duration', dest='duration', action='store', type='float', default=24*60*60, help='Duration of simulation (in seconds) (default: %default)')
    parser.add_option('', '--max_wait', dest='max_wait', action='store', type='float', default=180, help='Maximum wait for completed service (in seconds) (default: %default)')
    parser.add_option('', '--instance_type', dest='instance_type', action='store', type='choice', choices=COST_MODEL.keys(), default='m1.small', help='EC2 instance type [default: %default]')
    parser.add_option('', '--latency', dest='latency', action='store', type='float', default=90, help='Time it takes for new servers to spin up (in seconds) (default: %default)')
    parser.add_option('', '--rate', dest='rate', action='store', type='float', default=0.25, help='Arrival rate of jobs per second (default: %default)')

    si_help = \
'''You can modify the base rate of job arrivals as a function of
  time by specifying the parameters of a sinusoid which will be
  multiplied by the base rate in the following way:

     newrate = (sin(time * 2 * PI / period + 2 * PI * phase) * amplitude + offset) * rate

  Negative values are clamped to zero.

  The default is a constant rate of arrivals with no dependence on time.

  Sinusoidal rate modification parameters'''

    si_group = optparse.OptionGroup(parser, si_help)
    si_group.add_option('', '--si_period', dest='si_period', action='store', type='float', default=86400.0, help='Period (in seconds) (default: %default)')
    si_group.add_option('', '--si_phase', dest='si_phase', action='store', type='float', default=0.0, help='Phase (as a fraction of the period) (default: %default)')
    si_group.add_option('', '--si_amplitude', dest='si_amplitude', action='store', type='float', default=0.0, help='Amplitude (default: %default)')
    si_group.add_option('', '--si_offset', dest='si_offset', action='store', type='float', default=1.0, help='Offset (default: %default)')
    parser.add_option_group(si_group)

    pr_help = \
'''You can also modify the rate of job arrivals as a function of time
  by specifying a piece-wise profile of the rate (in jobs / second)
  vs. time (in seconds) so that arbitrary functions of load vs. time
  can be simulated. If this option is specified the rate given by the
  '--rate' flag is ignored. For example, if you want a load profile
  where the rate starts at 0, increases to 2 jobs / second in 30
  minutes, stays at 2 jobs / second for 30 minutes and then ramps back
  down to 0 in 30 minutes, the profile specification would look like
  this:

    --profile '[(0,0),(1800,2),(3600,2),(5400,0)]'

  The program actually sets a lower bound for arrival rate at 0.01
  jobs per second in runs where the arrival rate is set with a
  profile.

  Profile specification parameters'''

    pr_group = optparse.OptionGroup(parser, pr_help)
    pr_group.add_option('', '--profile', dest='profile', action='store', type='string', default='', help='Rate (jobs / second) vs. time (in seconds) as piece-wise profile')
    parser.add_option_group(pr_group)

    as_group = optparse.OptionGroup(parser, 'AWS AutoScaling parameters')
    as_group.add_option('', '--as_period', dest='as_period', action='store', type='int', default=60, help='The period associated with the metric statistics in seconds (default: %default)')
    as_group.add_option('', '--as_breach_duration', dest='as_breach_duration', action='store', type='int', default=240, help='The amount of time in seconds used to evaluate and determine if a breach is occurring (default: %default)')
    as_group.add_option('', '--as_min_size', dest='as_min_size', action='store', type='int', default=1, help='Minimum size of group (default: %default)')
    as_group.add_option('', '--as_max_size', dest='as_max_size', action='store', type='int', default=50, help='Maximum size of the group (default: %default)')
    as_group.add_option('', '--as_cooldown', dest='as_cooldown', action='store', type='int', default=240, help='The amount of time after a scaling activity completes before any further trigger-related scaling activities can start (default: %default)')
    as_group.add_option('', '--as_lower_threshold', dest='as_lower_threshold', action='store', type='int', default=70, help='The lower limit for the metric (default: %default)')
    as_group.add_option('', '--as_lower_breach_scale_increment', dest='as_lower_breach_scale_increment', action='store', type='int', default=-1, help='The incremental amount to use when performing scaling activities when the lower threshold has been breached (default: %default)')
    as_group.add_option('', '--as_upper_threshold', dest='as_upper_threshold', action='store', type='int', default=95, help='The upper limit for the metric (default: %default)')
    as_group.add_option('', '--as_upper_breach_scale_increment', dest='as_upper_breach_scale_increment', action='store', type='int', default=1, help='The incremental amount to use when performing scaling activities when the upper threshold has been breached (default: %default)')
    parser.add_option_group(as_group)

    options, args = parser.parse_args()
    if not args:
        parser.error('missing required argument')
    if len(args) != 1:
        parser.error('only one argument is permitted')
    file = args[0]
    try:
        with open(file, 'r') as _:
            pass
    except Exception, e:
        parser.error(str(e))

    if options.as_period % 60 != 0:
        parser.error('as_period must be a multiple of 60')
    if options.as_breach_duration % 60 != 0:
        parser.error('as_breach_duration must be a multiple of 60')

    if options.profile:
        try:
            profile = eval(options.profile)
            assert profile, 'profile is empty'
            assert isinstance(profile, list) or isinstance(profile, tuple), 'profile is not a sequence'
            assert len(profile) >= 2, 'profile is not a sequence of length 2 or larger'
            for i in profile:
                assert isinstance(i, list) or isinstance(i, tuple), 'item is not a sequence'
                assert len(i) == 2, 'item is not a sequence of length 2'
                assert isinstance(i[0], int) or isinstance(i[0], float), 'item first element is not numeric'
                assert isinstance(i[1], int) or isinstance(i[1], float), 'item second element is not numeric'
                assert i[1] >= 0, 'item second element is less than zero'
            options.profile = profile
        except Exception, e:
            parser.error('evaluation of profile failed: %s' % str(e))

    #
    # replace original imports with ones that trace
    #
    if options.trace:
        from SimPy.SimulationTrace import Process, Monitor, initialize, hold, passivate, activate, reactivate, simulate, now

    #
    # use a seed if set, otherwise use some OS source as the seed
    #
    if options.seed:
        random.seed(options.seed)
    else:
        random.seed()

    initialize()

    #
    # start up initial servers
    #
    for _ in range(options.capacity):
        s = Server(options.max_wait, options.instance_type, options.latency)
        activate(s, s.execute())

    e = Sources('Sources')
    activate(e, e.execute(options.rate, file, options.latency,
                          options.si_period,
                          options.si_amplitude,
                          options.si_phase,
                          options.si_offset,
                          options.profile,
                          options.rate))

    w = Watcher(options.max_wait, options.instance_type, options.latency,
                options.as_period,
                options.as_breach_duration,
                options.as_min_size,
                options.as_max_size,
                options.as_cooldown,
                options.as_lower_threshold,
                options.as_lower_breach_scale_increment,
                options.as_upper_threshold,
                options.as_upper_breach_scale_increment)
    activate(w, w.execute())

    simulate(until=options.duration)
    Server.total()
    Server.total_capacity.observe(len(Server.busy) + len(Server.idle))

    print
    print "server utilization                      :%6.2f%%" % Watcher.cpu_utilization.timeAverage()
    count = 0
    for i, y in itertools.izip(itertools.count(), sorted(Server.time_in_system.yseries())):
        count = i + 1
        if y > 30.0:
            break
    print "percentage of requests served within 30s:%6.2f%%" % (count * 100.0 / (Server.jobs_timed_out + Server.jobs_processed))
    print "percentage timed out (%3d seconds)      :%6.2f%%" % (options.max_wait, Server.jobs_timed_out * 100.00 / (Server.jobs_timed_out + Server.jobs_processed))
    print "server time wasted                      :%6.2f%%" % (Server.server_time_wasted * 100.0 / Server.server_time_used)
    print "mean time in system (seconds)           : %.2f" % Server.time_in_system.mean()
    print "total jobs processed                    : %d" % Server.jobs_processed
    print "total jobs timed out (%3d seconds)      : %d" % (options.max_wait, Server.jobs_timed_out)
    print "server cost                             : $%.2f" % Server.total_cost
    print "jobs processed per $1 server cost       : %d" % (Server.jobs_processed / Server.total_cost)

    if options.plot:
        plt = SimPlot()
        plt.root.title('Simulation Output')
        step = plt.makeStep(Server.total_capacity, color='blue')
        line = plt.makeLine(Watcher.arrivals_mon, color='green')
        obj1 = plt.makeGraphObjects([step])
        obj2 = plt.makeGraphObjects([line])
        frame = Frame(plt.root)
        graph1 = plt.makeGraphBase(frame, 500, 300, title='Total Capacity', xtitle='time (seconds)', ytitle='servers')
        graph1.pack(side=TOP, fill=BOTH, expand=YES)
        graph1.draw(obj1, xaxis=(0, options.duration))
        graph2 = plt.makeGraphBase(frame, 500, 300, title='Arrival Rate', xtitle='time (seconds)', ytitle='jobs/min')
        graph2.pack(side=TOP, fill=BOTH, expand=YES)
        graph2.draw(obj2, xaxis=(0, options.duration))
        frame.pack()
        plt.mainloop()
