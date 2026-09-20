# The account server. Built, not deployed by this repo -- see DEPLOY.md.
#
# Two things here are load-bearing rather than boilerplate.
#
# `cci-server migrate` runs as its own step at start, before `run`, for the
# same reason the launchd job leads with `cci init`: a release that adds a
# migration must not leave a server refusing every request while the error
# sits in a log nobody reads. It is idempotent and additive.
#
# The image binds 0.0.0.0 because a container's localhost is reachable by
# nothing. That is the one place the "make somebody type the address it
# listens on" default in `__main__.py` has to be overridden, and doing it
# here -- rather than changing the default -- keeps a laptop-run server
# private by accident rather than by remembering.

FROM python:3.12-slim AS build

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations

# A wheel, so the force-include that carries `migrations/` into the package
# actually runs. Installing the source tree directly would find the .sql
# files by accident of layout here and not at all in a slimmer image --
# which is the exact failure the client shipped and had to fix.
RUN pip install --no-cache-dir build hatchling \
    && python -m build --wheel --no-isolation --outdir /dist .


FROM python:3.12-slim

# Not root. The server holds other people's filesystem paths; a container
# escape should not also be a root escape.
RUN useradd --create-home --uid 10001 cci
WORKDIR /srv

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

USER cci

# Proof the wheel carries its schema. An image that cannot find its
# migrations creates an empty database and reports success, and the first
# person to notice is a user whose push 500s.
RUN python -c "import pathlib, cci_server; \
    d = pathlib.Path(cci_server.__file__).parent / 'migrations'; \
    n = len(list(d.glob('*.sql'))); \
    assert n >= 4, f'image has {n} migrations, expected the full set'; \
    print(f'migrations in image: {n}')"

ENV PORT=8788
EXPOSE 8788

# `sh -c` so $PORT expands: the platform assigns it and the app must listen
# on what it was given, not on what the Dockerfile guessed.
CMD ["sh", "-c", "cci-server migrate && cci-server run --host 0.0.0.0 --port ${PORT}"]
