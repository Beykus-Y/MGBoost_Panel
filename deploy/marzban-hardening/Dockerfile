# syntax=docker/dockerfile:1

# Production ran this exact Marzban image before PH1-07.  Keep the digest
# immutable so dependency hardening cannot silently upgrade Marzban itself.
ARG MARZBAN_BASE_IMAGE=gozargah/marzban@sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d

FROM ${MARZBAN_BASE_IMAGE} AS ph1-07

COPY deploy/marzban-hardening/python-multipart.lock /tmp/python-multipart.lock
RUN python -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        --no-deps \
        --require-hashes \
        -r /tmp/python-multipart.lock \
    && python -c "import importlib.metadata as m; assert m.version('python-multipart') == '0.0.32'" \
    && rm -f /tmp/python-multipart.lock

LABEL fun.beykus.mgboost.phase1="PH1-07" \
      fun.beykus.mgboost.marzban-base="0.8.4@sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d" \
      fun.beykus.mgboost.python-multipart="0.0.32"
