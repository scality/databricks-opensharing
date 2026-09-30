# The setup image: the sharing server plus a web page that configures it.
#
# Built FROM the server image produced by the Dockerfile at the repository root, so the
# two images always carry the same server build. The page (setup/*.py, stdlib Python)
# browses the bucket for Delta tables, writes the two configuration files, starts the
# server, verifies the deployment and hands out the recipient's .share file.
#
# Build:  docker build --build-arg SERVER_IMAGE=<server image> -f setup/Dockerfile -t <tag> .
# Run:    see setup/README.md
#
# linux/amd64 only, like the server image it derives from. SERVER_IMAGE defaults to the
# tag the root Dockerfile's own build line produces; CI passes the image it just built.
ARG SERVER_IMAGE=databricks-opensharing:dev
FROM ${SERVER_IMAGE}

# The tag this image was built from, reported at start and on /api/status.
ARG SETUP_VERSION=dev
ENV SETUP_VERSION=${SETUP_VERSION}

# eclipse-temurin:17-jre ships no python3. ca-certificates gives the page the system
# roots it verifies public endpoints against; keytool comes with the JRE. Root only for
# the install: the image runs as uid 1000 below.
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /config && chown 1000:1000 /config && chmod 0700 /config

COPY setup/ /opt/setup/

# The page binds every interface of the CONTAINER; what reaches it is decided by the
# port you publish. `-p 127.0.0.1:9481:9481` keeps it on the host's loopback.
# Ports (deploy/ports.yaml): 9480 sharing server, 9481 page, 9482 metrics and probes.
# SERVER_PORT is deliberately not set here: unset, a /config rendered on another port
# keeps that port (setup/render.py resolve_server_port).
ENV CONFIG_DIR=/config \
    SETUP_BIND=0.0.0.0 \
    SETUP_PORT=9481 \
    METRICS_PORT=9482
EXPOSE 9480 9481 9482

# /config is a volume so the rendered configuration survives a container replacement.
# It exists in the image owned by uid 1000, so a new named volume starts with that owner.
VOLUME ["/config"]

# The page and the server it supervises run as uid/gid 1000, never root.
USER 1000:1000

ENTRYPOINT ["python3", "/opt/setup/entrypoint.py"]
