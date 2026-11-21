# Same base image the organizers labeled with (linux/amd64).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The pinned sqlglot commit, straight from sqlglot's own repository. Only its
# source is used (to read patches against it); nothing is installed or run.
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz

COPY selector.py build_index.py prepare.py covmap.json.gz importset.json.gz /app/
RUN python /app/prepare.py /tmp/sqlglot.tar.gz /app/src && rm /tmp/sqlglot.tar.gz

CMD ["python", "/app/selector.py"]
