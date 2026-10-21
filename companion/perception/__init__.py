"""People, speakers and what is going on in the room (see docs/people.md).

The pieces, each usable on its own and all deterministic given their inputs:

    registry   people the companion knows: face and voice exemplars (data/people)
    recording  a recorded session (video, audio, hand labels) for the replay harness
    replay     feeds a recording through the perception steps offline and scores it

Identity, speaker attribution and routing are decided here, on the server, by
plain rules; the agent receives labels (names, roles), never embeddings.
"""
