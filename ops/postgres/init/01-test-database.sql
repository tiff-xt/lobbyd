-- Runs once, when the Postgres volume is first created.
-- A separate database for the test suite, which wipes everything it touches.
CREATE DATABASE lobbyd_test OWNER lobbyd;
