"""What a model call was producing when it stalled or failed.

A stream that stops mid-call is otherwise opaque. The trace records, per call, how
many chunks arrived, what the last one was (reasoning, text or tool arguments, and
for which tool) and how long the stream was idle, so a stall points at its cause.
"""
import time


class StreamTrace:
    def __init__(self):
        self.call = None

    def start(self):
        """Begin a model call; returns the previous call's facts if it never finished."""
        unfinished = self.pending()
        self.call = {'started': time.monotonic(), 'last_at': None, 'chunks': 0, 'last': None, 'tool': None}
        return unfinished

    def chunk(self, chunk):
        if self.call is None:
            self.start()
        call = self.call
        call['chunks'] += 1
        call['last_at'] = time.monotonic()
        tool_chunks = getattr(chunk, 'tool_call_chunks', None) or []
        if tool_chunks:
            call['last'] = 'tool_args'
            call['tool'] = next((c.get('name') for c in tool_chunks if c.get('name')), call['tool'])
        elif isinstance(getattr(chunk, 'content', None), str) and chunk.content:
            call['last'] = 'text'
        elif (getattr(chunk, 'additional_kwargs', None) or {}).get('reasoning_content'):
            call['last'] = 'reasoning'

    def end(self):
        if self.call is not None:
            self.call['ended'] = True

    def pending(self):
        call = self.call
        if call is None or call.get('ended'):
            return None
        now = time.monotonic()
        return {'chunks': call['chunks'], 'last_chunk': call['last'], 'tool': call['tool'],
                'elapsed_s': round(now - call['started'], 1),
                'idle_s': round(now - (call['last_at'] or call['started']), 1)}
