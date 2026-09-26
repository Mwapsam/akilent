# Same Postgres major version as the `db` service, so pg_dump/pg_restore always match the server.
FROM postgres:16-alpine

RUN apk add --no-cache aws-cli

COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh

ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["loop"]
