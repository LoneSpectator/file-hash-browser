# Hash plugin directory

This optional directory is loaded only when `plugins.directory` points to it.
Plugin files are trusted administrator code and cannot be uploaded or selected
through the Web API. Copy `example_blake2b.py.example` to a `.py` file to enable
the example, then add its ID to `plugins.enabled_algorithms` when that allowlist
is configured.

