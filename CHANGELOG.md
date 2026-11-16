# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/) and this project adheres to [Semantic Versioning](http://semver.org/).

## Unreleased

- Preserve explicit empty-string arguments in message helpers and TCP clients while keeping omitted values argument-free.
- Pin each dispatched message to a single dispatcher configuration version, so a handler mapping or unmapping addresses while a message is being processed cannot change that message's handlers; the change only affects later messages.
- Invoke each matched handler at most once per message even when the same `Handler` instance is reachable through several matching addresses.
- Isolate handler failures: a raising callback is recorded in `Dispatcher.failures` (one `DispatchFailure` per failed subscription, cleared with `clear_failures`) without interrupting the other matched handlers. An optional `failure_buffer_size` bounds the retained records.

## [1.10.2]

- Better asyncio handling, ability to ignore timetags for future messages

## [1.10.0]

- Added TCPDispatchClient to tcp_client
- Fixed TPC dispatcher type annotations
- Fixed regexp matching
- Fixed timestamp parsing > 24h
- Switched to uv

## [1.9.3]

- Reinstate mistakenly deleted package type annotations on main branch (again)

## [1.9.2]

- Reinstate mistakenly deleted package type annotations on main branch

## [1.9.1]

- Reinstate mistakenly deleted package type annotations

## [1.9.0]

- Added TCP Client and Server support for OSC 1.0 and OSC 1.1 formats, with support for sending responses to the client
- Added response support to the existing UDP Client and Server code

## [1.8.3]

- Using trusted publisher setup to publish to pypi

## [1.8.2]

- Changed packaging method to pypa/build
- Removed pygame example to simplify dependencies

## [1.8.1]

- Add options to UdpClient to force the use of IPv4 when IPv6 is available and vice versa

- Add support for arguments with Nil type

### [1.8.0]

- Support for sending and receiving Int64 datatype (`h`).

## [1.7.7]

###  Fixed

Flaky NTP test

## [1.7.6]

### Added

-  Support for python 3.7 and 3.8.

-  Releasing wheel on top of source package.

## [1.7.4]

### Added

- Support for sending nil values.

- IPV6 support to UDPClient.

### Fixed

Timestamp parsing
