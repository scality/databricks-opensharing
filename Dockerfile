# Delta Sharing / OpenSharing server, built from the source in THIS repository.
#
# It deliberately does not clone upstream: the presigner fix is a commit here, so the
# image and the code that produced it cannot drift apart.
#
# Build:  docker build -t databricks-opensharing:dev .
# Run:    see README.md

# ── Stage 1: compile ─────────────────────────────────────────────────────────
FROM sbtscala/scala-sbt:eclipse-temurin-17_1.x AS builder

WORKDIR /build

# Dependency resolution is the slow half of this build and changes far less often than
# the source, so copy the build definition first and let the layer cache keep it.
COPY build.sbt version.sbt scalastyle-config.xml ./
COPY project ./project
RUN sbt -batch update || true

COPY . .

# `sbt server/universal:packageBin` emits a zip under server/target/universal/ whose name
# carries the version. Glob it rather than hard-coding, so a version bump needs no edit here.
RUN sbt --batch "server/universal:packageBin" && \
    ZIPFILE="$(ls "$PWD"/server/target/universal/delta-sharing-server-*.zip | head -n1)" && \
    test -n "$ZIPFILE" && \
    mkdir -p /dist-staging && cd /dist-staging && \
    jar xf "$ZIPFILE" && \
    DIST_DIR="$(ls -d /dist-staging/delta-sharing-server-*/ | head -n1)" && \
    mv "$DIST_DIR" /dist
# `jar` is used rather than `unzip`, which the sbtscala image does not ship.

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM eclipse-temurin:17-jre

WORKDIR /opt/delta-sharing-server

COPY --from=builder /dist .

# Two fixups, both load-bearing:
#  1. `jar xf` does not preserve POSIX exec bits, so the launcher arrives non-executable.
#  2. The launcher puts `<dist>/../conf` on the classpath, so Hadoop reads core-site.xml as
#     a CLASSPATH RESOURCE from the distribution's conf/ — NOT from HADOOP_CONF_DIR.
#     Symlinking it at /config lets one mounted /config supply both the server YAML
#     (passed with --config) and core-site.xml.
RUN chmod +x bin/delta-sharing-server && \
    ln -sf /config/core-site.xml conf/core-site.xml

# The presigner resolves credentials through the AWS default chain rather than the
# fs.s3a.* keys, so AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION must be present
# in the process environment as well as in core-site.xml. Left unset on purpose — supply
# them at run time; baking credentials into an image layer would publish them.

EXPOSE 8080
ENTRYPOINT ["bin/delta-sharing-server"]
CMD ["--config", "/config/delta-sharing-server.yaml"]
