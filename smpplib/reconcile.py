#
# smpplib -- SMPP Library for Python
# Copyright (c) 2005 Martynas Jocius <mjoc@akl.lt>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
Request fact table for SMPP request/response reconciliation.

Captured SMPP traffic may be out of order, and sequence numbers restart
from one whenever a transport connection is rebuilt.  Matching responses
by sequence number alone therefore conflates PDUs from different
generations of the sequence space.

The fact table keeps a per-session "in-flight window": a response may only
complete a request that is currently outstanding in the same session and
has the same sequence number and command.  A response that cannot be
matched inside the legal in-flight window is reported as an orphan of
unknown ownership instead of being force-matched, so a late response of
an old session can never complete a same-numbered request of a new
session, nor can a wrap-around reuse complete the wrong generation.
"""


# Lifecycle statuses used by the rows of the request fact table.
REQUEST_PENDING = 'pending'
REQUEST_ANSWERED = 'answered'
REQUEST_REJECTED = 'rejected'
REQUEST_UNKNOWN = 'unknown'


def base_command(command):
    """Return the request command name a response command corresponds to."""
    if command.endswith('_resp'):
        return command[:-len('_resp')]
    return command


class RequestRecord(object):
    """One row of the request fact table.

    Rows describing an orphan response keep ``request_pdu`` unset and the
    response command name in ``command``.
    """

    def __init__(self, session_id, sequence, command, status,
                 request_pdu=None, response_pdu=None, response_status=None):
        self.session_id = session_id
        self.sequence = sequence
        self.command = command
        self.status = status
        self.request_pdu = request_pdu
        self.response_pdu = response_pdu
        self.response_status = response_status

    def __repr__(self):
        return (
            'RequestRecord(session_id={!r}, sequence={!r}, '
            'command={!r}, status={!r})'.format(
                self.session_id, self.sequence, self.command, self.status))


class RequestFactTable(object):
    """Reconcile SMPP requests with responses across disorder, sequence
    wrap-around and connection restarts."""

    FIRST_SESSION = 1

    def __init__(self):
        self._session_id = self.FIRST_SESSION
        # In-flight window keyed by (session id, sequence, request command).
        self._window = {}
        # All fact-table rows in chronological order.
        self._records = []

    @property
    def session_id(self):
        """Session (connection generation) currently being recorded into."""
        return self._session_id

    def record_request(self, pdu, session_id=None):
        """Register a request PDU as in-flight and add a pending row."""
        if session_id is None:
            session_id = self._session_id

        key = (session_id, pdu.sequence, pdu.command)
        if key in self._window:
            raise ValueError(
                'Sequence number %d of command %r is already in-flight in '
                'session %d; a sequence number must not be reused while the '
                'request carrying it is still outstanding'
                % (pdu.sequence, pdu.command, session_id))

        record = RequestRecord(
            session_id=session_id,
            sequence=pdu.sequence,
            command=pdu.command,
            status=REQUEST_PENDING,
            request_pdu=pdu,
        )
        self._window[key] = record
        self._records.append(record)
        return record

    def record_response(self, pdu, session_id=None):
        """Match a response against a session's in-flight window.

        ``session_id`` pins the response to the connection generation on
        which it was observed; it defaults to the current session.  Returns
        the completed request record (answered or rejected), or an orphan
        record of unknown ownership when no in-flight request of that
        session, sequence number and command exists.  Non-response PDUs
        (e.g. server-initiated ``deliver_sm``) are ignored.
        """
        if not pdu.is_response():
            return None

        if session_id is None:
            session_id = self._session_id

        key = (session_id, pdu.sequence, base_command(pdu.command))
        record = self._window.get(key)

        if record is None:
            orphan = RequestRecord(
                session_id=session_id,
                sequence=pdu.sequence,
                command=pdu.command,
                status=REQUEST_UNKNOWN,
                response_pdu=pdu,
                response_status=pdu.status,
            )
            self._records.append(orphan)
            return orphan

        del self._window[key]
        record.response_pdu = pdu
        record.response_status = pdu.status
        record.status = REQUEST_REJECTED if pdu.is_error() else REQUEST_ANSWERED
        return record

    def close_open_requests(self):
        """Mark every in-flight request as unknown and empty the window.

        Requests still outstanding when the transport goes away can never
        be answered in this session, so they leave the window without
        being completed.
        """
        for record in self._window.values():
            record.status = REQUEST_UNKNOWN
        self._window.clear()

    def start_new_session(self):
        """Finalize the old session and open a new one.

        The historical rows are preserved but the in-flight window is
        cleared, so late responses of the old session cannot complete
        requests of the new session even when sequence numbers repeat.
        """
        self.close_open_requests()
        self._session_id += 1
        return self._session_id

    def begin_connection(self):
        """Start a new session on (re)connect while keeping the first
        session at one when nothing has been recorded yet."""
        if self._records or self._window:
            self.start_new_session()

    def build_fact_table(self):
        """Return all recorded rows in chronological order."""
        return list(self._records)

    def pending_requests(self):
        """Requests still waiting for a response."""
        return [record for record in self._records if record.status == REQUEST_PENDING]

    def answered_requests(self):
        """Requests completed by a successful (ESME_ROK) response."""
        return [record for record in self._records if record.status == REQUEST_ANSWERED]

    def rejected_requests(self):
        """Requests completed by an error-status response."""
        return [record for record in self._records if record.status == REQUEST_REJECTED]

    def unknown_records(self):
        """Rows that could not be attributed: lost in-flight requests or
        orphan responses."""
        return [record for record in self._records if record.status == REQUEST_UNKNOWN]

    def stats(self):
        """Return row counts per status plus the in-flight window size."""
        return {
            'session_id': self._session_id,
            REQUEST_PENDING: len(self.pending_requests()),
            REQUEST_ANSWERED: len(self.answered_requests()),
            REQUEST_REJECTED: len(self.rejected_requests()),
            REQUEST_UNKNOWN: len(self.unknown_records()),
            'in_flight': len(self._window),
        }
