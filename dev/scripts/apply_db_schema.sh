#!/bin/bash

cd "$(dirname "$0")"
psql -1 -h "$DATABASE_HOST" -p "$DATABASE_PORT" -U "$DATABASE_USER" -d "$DATABASE_NAME" -f ../sql/schema_relational.sql
