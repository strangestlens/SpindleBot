"""External collection sources.

Each provider adapts one service's output to `core.collection.CollectionItem`.
A provider is split in two on purpose: an impure client that talks to the
service, and a pure transformer that maps its payload to items. The transformer
is where every source-specific quirk lives, and it is testable against a
recorded fixture with no network.
"""
