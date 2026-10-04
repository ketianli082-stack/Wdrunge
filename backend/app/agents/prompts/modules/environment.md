## Project environment
The handoff's `environment` is the single source for ports, addresses and start
commands: host conventions, ports reserved by the platform, registered services and
project commands. Never pick a port by habit or use a reserved one; refer to services
by name. Each service shows whether it is running now (verified only means it once
served). Use a running service as it is; start one only when it is stopped, and
restart it with keep_running=true (which replaces the running copy) only after
changing its code or when it is marked stale. For a new server,
collaboration_register_service gives it a free port (omit port); start it with
execute_command(background=true, ready_port=<port>). Only services a task declares
stay running after delivery, from the project root. A service sees only the workspace
it runs from: test undelivered changes on your own copy on a newly registered port. manage_process reads logs of, or stops, any
running project service by name. Put resolved addresses
in deliverables only where users need them (README, configuration) as plain
values; never explain the registry or reserved ports.
