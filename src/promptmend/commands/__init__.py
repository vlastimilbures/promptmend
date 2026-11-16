"""The management commands: espanso, setup, config, secrets, profiles, stats, history,
doctor and ui.

Each module holds thin Typer wrappers over the services (config_store, deploy, history,
profiles, doctor, smoke). cli.py loads a module only when its command runs or --help lists it,
so improve and persona never import this package.
"""
