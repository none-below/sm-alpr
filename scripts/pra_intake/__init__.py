"""PRA evidence intake: stream fetched files to a staging bucket; a Lambda
copies each one, write-once, into a content-addressed evidence bucket.

schema.py is the contract the upload library, the ingest Lambda and the sweep
share. See README.md.
"""
