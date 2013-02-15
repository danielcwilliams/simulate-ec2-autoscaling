simulate-ec2-autoscaling
========================

SimPy-based Python discrete event simulation of AWS EC2 Autoscaling for a job queue

	Usage: as-sim.py [options] service-distribution-pathname
	
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
	computations.
	
	Options:
	  -h, --help            show this help message and exit
	  --seed=SEED           PRNG seed (default: 0 means use an OS source)
	  --capacity=CAPACITY   Set initial number of servers (default: 1)
	  --trace               Trace simulation activity (very detailed)
	  --plot                Plot capacity and load vs. time
	  --duration=DURATION   Duration of simulation (in seconds) (default: 86400)
	  --max_wait=MAX_WAIT   Maximum wait for completed service (in seconds)
	                        (default: 180)
	  --instance_type=INSTANCE_TYPE
	                        EC2 instance type [default: m1.small]
	  --latency=LATENCY     Time it takes for new servers to spin up (in seconds)
	                        (default: 90)
	  --rate=RATE           Arrival rate of jobs per second (default: 0.25)
	
	  You can modify the base rate of job arrivals as a function of
	  time by specifying the parameters of a sinusoid which will be
	  multiplied by the base rate in the following way:
	
	     newrate = (sin(time * 2 * PI / period + 2 * PI * phase) * amplitude + offset) * rate
	
	  Negative values are clamped to zero.
	
	  The default is a constant rate of arrivals with no dependence on time.
	
	  Sinusoidal rate modification parameters:
	    --si_period=SI_PERIOD
	                        Period (in seconds) (default: 86400.0)
	    --si_phase=SI_PHASE
	                        Phase (as a fraction of the period) (default: 0.0)
	    --si_amplitude=SI_AMPLITUDE
	                        Amplitude (default: 0.0)
	    --si_offset=SI_OFFSET
	                        Offset (default: 1.0)
	
	  You can also modify the rate of job arrivals as a function of time
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
	
	  Profile specification parameters:
	    --profile=PROFILE   Rate (jobs / second) vs. time (in seconds) as piece-
	                        wise profile
	
	  AWS AutoScaling parameters:
	    --as_period=AS_PERIOD
	                        The period associated with the metric statistics in
	                        seconds (default: 60)
	    --as_breach_duration=AS_BREACH_DURATION
	                        The amount of time in seconds used to evaluate and
	                        determine if a breach is occurring (default: 240)
	    --as_min_size=AS_MIN_SIZE
	                        Minimum size of group (default: 1)
	    --as_max_size=AS_MAX_SIZE
	                        Maximum size of the group (default: 50)
	    --as_cooldown=AS_COOLDOWN
	                        The amount of time after a scaling activity completes
	                        before any further trigger-related scaling activities
	                        can start (default: 240)
	    --as_lower_threshold=AS_LOWER_THRESHOLD
	                        The lower limit for the metric (default: 70)
	    --as_lower_breach_scale_increment=AS_LOWER_BREACH_SCALE_INCREMENT
	                        The incremental amount to use when performing scaling
	                        activities when the lower threshold has been breached
	                        (default: -1)
	    --as_upper_threshold=AS_UPPER_THRESHOLD
	                        The upper limit for the metric (default: 95)
	    --as_upper_breach_scale_increment=AS_UPPER_BREACH_SCALE_INCREMENT
	                        The incremental amount to use when performing scaling
	                        activities when the upper threshold has been breached
	                        (default: 1)
