# ISO15118

Python Implementation of the ISO 15118 -2 [^1] and -20 [^2] protocols

## How to fire it up :fire:

The ISO 15118 -2 and -20 code lives in the directory `iso15118`.
The primary dependencies to install the project are the following:

> - Linux Distro (Non-Linux Systems are not supported)
> - Poetry [^3]
> - Python >= 3.7

There are two recommended ways of running the project:

1. Building and running the docker file:

   ```bash
   $ make build
   $ make dev
   ```

2. Local Installation

   Install JRE engine with the following command:

   ```bash
   sudo apt update && sudo apt install -y default-jre

   ```

   The JRE engine is only a temporary requirement until we replace the Java-based
   EXI codec (EXIficient)[^4] with our own Rust-based EXI codec.

   Install the module using `poetry` and run the main script related
   to the EVCC or SECC instance you want to run. Switch to the iso15118 directory
   and run:

   ```bash
   $ poetry update
   $ poetry install
   $ python iso15118/secc/start_secc.py # or python iso15118/evcc/start_evcc.py
   ```

   For convenience, the Makefile, present in the project, helps you to run these
   steps. Thus, in the terminal run:

   ```bash
   $ make install-local
   $ make run-secc
   ```

   This will call the poetry commands above and run the start script of the
   secc.

Option number `1` has the advantage of running within Docker, where everything
is fired up automatically, including certificates generation, tests and linting.

Also both SECC and EVCC are spawned, automatically.

For option number `2`, the certificates need to be provided. The project includes
a script to help on the generation of -2 and -20 certificates. This script
is located under `iso15118/shared/pki/` directory and is called `create_certs.sh`.
The following command provides a helper for the script usage:

```bash
$ ./create_certs.sh -h
```

---

**IPv6 WARNING**

For the system to work locally, the network interface to be used needs to have
an IPv6 local-link address assigned.

For Docker, the `docker-compose.yml` was configured to create an `IPv6` network
called `ipv6_net`, which enables the containers to acquire a local-link address,
which is required to establish an ISO 15118 communication. This configuration is
fine if the user wants to test, in isolation, the EVCC and SECC and allow ISO 15118
communication. This configuration works for both Linux and BSD systems.

However, the usage of an internal `ipv6_net` network, in Docker, does not allow the
host to reach link-local addresses. This would pose a problem, as it would require
the application to use the global-link address, which is not supported by ISO 15118.

The solution is to use the `network_mode: host` feature of Docker, which replicates
the host network topology within the Docker world, i.e. the containers and the
host share the same network. This way, Docker can directly access the virtual
network interface created by the HomePlug Green PHY module, making it possible
to use the local-link address.

Currently, `network_mode: host` just works within Linux environments [^5] [^6].
Since the Switch team relies mostly on MacOS and this project is on a development stage,
`network_mode` is not used by default, but it is possible to use it if the contents of the
file `docker-compose-host-mode.yml` are copied to the main compose file, `docker-compose.yml`.
In that case, it is advised to back up the compose file.

---

## Environment Settings

Finally, the project includes a few configuration variables, whose default
values can be modified by setting them as environmental variables.
The following table provides a few of the available variables:

| ENV                 | Default Value                | Description                                                                                                                                                     |
| ------------------- | ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| NETWORK_INTERFACE   | `eth0`                       | HomePlug Green PHY Network Interface from which the high-level communication (HLC) will be established                                                          |
| SECC_CONTROLLER_SIM | `False`                      | Whether or not to simulate the SECC Controller Interface                                                                                                        |
| SECC_ENFORCE_TLS    | `False`                      | Whether or not the SECC will enforce a TLS connection                                                                                                           |
| EVCC_CONTROLLER_SIM | `False`                      | Whether or not to simulate the EVCC Controller Interface                                                                                                        |
| EVCC_USE_TLS        | `True`                       | Whether or not the EVCC signals the preference to communicate with a TLS connection                                                                             |
| EVCC_ENFORCE_TLS    | `False`                      | Whether or not the EVCC will only accept TLS connections                                                                                                        |
| PKI_PATH            | `<CWD>/iso15118/shared/pki/` | Path for the location of the PKI where the certificates are located. By default, the system will look for the PKI directory under the current working directory |
| REDIS_HOST          | `localhost`                  | Redis Host URL                                                                                                                                                  |
| REDIS_PORT          | `6379`                       | Redis Port                                                                                                                                                      |
| LOG_LEVEL           | `INFO`                       | Level of the Python log service                                                                                                                                 |

The project includes a few environmental files, in the root directory, for
different purposes:

- `.env.dev.docker` - ENV file with development settings, tailored to be used with docker
- `.env.dev.local` - ENV file with development settings, tailored to be used with
  the local host

If the user runs the project locally, e.g. using `$ make build && make run-secc`,
it is required to create a `.env` file, containing the required settings.

This means, if development settings are desired, one can simply copy the contents
of `.env.dev.local` to `.env`.

If Docker is used, the command `make run` will try to get the `.env` file;
The command `make dev` will fetch the contents of `.env.dev.docker` - thus,
in this case, the user does not need to create a `.env` file, as Docker will
automatically fetch the `.env.dev.docker` one.

The key-value pairs defined in the `.env` file directly affect the settings
present in `secc_settings.py` and `evcc_settings.py`. In these scripts, the
user will find all the settings that can be configured.

## Integration Test with an EV Simulator

Since the project includes both the SECC and EVCC side, it is possible to test
your application starting both services. Similar to the SECC, we can start the
EVCC side as follows:

```bash
$ make install-local
$ make run-evcc
```

This integration test was tested under:

- Linux - Ubuntu and Debian distros
- MacOs

[^1]: https://www.iso.org/standard/55366.html
[^2]: https://www.switch-ev.com/news-and-events/new-features-and-timeline-for-iso15118-20
[^3]: https://python-poetry.org/docs/#installation
[^4]: https://exificient.github.io/
[^5]: https://docs.docker.com/network/host/
[^6]: https://docs.docker.com/desktop/mac/networking/