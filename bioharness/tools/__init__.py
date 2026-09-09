"""In-process capabilities: python functions registered like any other tool.

Every entrypoint here takes (inputs, params, workdir) and returns a dict of
output port name -> path, matching what the manifest declares.
"""
