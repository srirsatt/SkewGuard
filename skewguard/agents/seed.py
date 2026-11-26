# system prompt for seed generation - data inserts for use in testing

SEED_SYSTEM_PROMPT = """\
Propose one row using field names and valid values from the old release’s model definition.
Follow the GET route to its view and choose values that make the view’s query return that row.
"""