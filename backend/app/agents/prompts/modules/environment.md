## Project environment
The handoff's `environment` is the single source for ports, addresses and start
commands: host conventions, ports reserved by the platform, registered services and
project commands. Never pick a port by habit or use a reserved one; refer to services
by name. Before starting a new server, collaboration_register_service gives it a free
port (omit port); start it with execute_command(background=true, ready_port=<port>)
and the platform marks it verified. Put resolved addresses in deliverables only where
users need them (README, configuration).
