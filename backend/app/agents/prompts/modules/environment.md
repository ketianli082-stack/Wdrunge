## Project environment
The handoff's `environment` is the single source for host conventions, ports,
addresses, start and project commands and reserved ports. Never pick a
port by habit or use a reserved one; refer to services by name. A service's running
state is live (verified only means it once served). Use a running service
as is; start it only when stopped, and restart it (keep_running=true) only after
changing its code or when marked stale. Register a new server with
collaboration_register_service before starting it. Only services a task declares
stay running after delivery, from the project root. A service sees only the workspace
it runs from: test undelivered changes on your own copy on a newly registered port.
`environment.connected` lists platform-provided services (the database): use their
env vars; never register, declare or start them. Put resolved addresses in
deliverables only where users need them (README, config) as plain values;
never explain the registry or reserved ports.
