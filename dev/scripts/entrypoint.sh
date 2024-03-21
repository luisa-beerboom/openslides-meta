#!/bin/bash

scripts/wait-for-database.sh
scripts/apply_db_schema.sh

exec "$@"
