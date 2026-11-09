import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

import pytest
import requests
from curl_cffi.requests.exceptions import HTTPError, RequestException
from pytest import MonkeyPatch

from shigoto.sources import ziprecruiter_detail as z
from shigoto.db import Store
from shigoto.enrich import _enrich
from shigoto.models import Job

URL = 'https://www.ziprecruiter.com/c/Acme/Job/Lab/-in-Toronto,ON?jid=1'
HTML = '<script type="application/ld+json">' + json.dumps(
    {'@type': 'JobPosting', 'description': '<p>Analyze samples.</p>'}) + '</script>'


def response(status: int = 200, text: str = HTML, *, flight: bool = False,
             location: str = '') -> Mock:
    result = Mock(status_code=status, url=URL, text=text)
    result.headers = {'Content-Type': 'text/x-component' if flight else 'text/html'}
    if location:
        result.headers['Location'] = location
    if status >= 400:
        result.raise_for_status.side_effect = HTTPError('blocked or gone', response=result)
    return result


def sessions(monkeypatch: MonkeyPatch, replies: list[Mock]) -> list[Mock]:
    clients = [Mock() for _ in replies]
    for client, reply in zip(clients, replies):
        client.get.return_value = reply
    monkeypatch.setattr(z, 'create_session', Mock(side_effect=clients))
    return clients


def test_challenge_then_flight_redirect_then_utf8_description(monkeypatch: MonkeyPatch) -> None:
    markup = '<p>Contrôle qualité, café et échantillons.</p>'
    stream = '0:{"jobDetails":{"htmlFullDescription":"$1"}}\n1:T' + f'{len(markup.encode()):x},' + markup
    clients = sessions(monkeypatch, [response(403), response(text='0:{"digest":'
        '"NEXT_REDIRECT;replace;/c/acme/Job/Lab?jid=1;308;"}', flight=True),
        response(text=stream, flight=True)])
    waits: list[float] = []
    monkeypatch.setattr('shigoto.sources.ziprecruiter_detail.time.sleep', waits.append)
    assert z.fetch_description(URL) == ('Contrôle qualité, café et échantillons.', '')
    assert waits == [2, 4]
    assert clients[0].get.call_args.kwargs['headers'] == {}
    for client in clients[1:]:
        assert client.get.call_args.kwargs['headers'] == {'RSC': '1'}
        assert parse_qs(urlparse(client.get.call_args.args[0]).query, keep_blank_values=True) == {
            'jid': ['1'], '_rsc': ['']}
    assert '/c/acme/Job/Lab?' in clients[2].get.call_args.args[0]
    for client in clients:
        assert client.get.call_args.kwargs['allow_redirects'] is False
        client.close.assert_called_once()


def test_http_redirect_is_paced_and_consumes_budget(monkeypatch: MonkeyPatch) -> None:
    clients = sessions(monkeypatch, [response(308, location='/c/canonical/Job/Lab?jid=1'), response()])
    waits: list[float] = []
    monkeypatch.setattr('shigoto.sources.ziprecruiter_detail.time.sleep', waits.append)
    assert z.fetch_description(URL) == ('Analyze samples.', '')
    assert waits == [2]
    assert clients[1].get.call_args.args[0] == 'https://www.ziprecruiter.com/c/canonical/Job/Lab?jid=1'


@pytest.mark.parametrize('failure', ['challenge', 'empty', 'transport', 'redirect_loop'])
def test_every_failure_and_redirect_shares_a_fixed_budget(monkeypatch: MonkeyPatch, failure: str) -> None:
    replies = [response(403) if failure == 'challenge' else response(text='Unrecognized page')
               for _ in range(z.MAX_REQUESTS)]
    if failure == 'redirect_loop':
        replies = [response(308, location=URL) for _ in range(z.MAX_REQUESTS)]
    clients = sessions(monkeypatch, replies)
    if failure == 'transport':
        for client in clients:
            client.get.side_effect = RequestException('network failure')
    waits: list[float] = []
    monkeypatch.setattr('shigoto.sources.ziprecruiter_detail.time.sleep', waits.append)
    with pytest.raises((requests.RequestException, ValueError)):
        z.fetch_description(URL)
    assert waits == [2, 4, 8]
    for client in clients:
        client.get.assert_called_once()
        client.close.assert_called_once()


@pytest.mark.parametrize('status', [404, 410])
def test_gone_response_is_terminal(monkeypatch: MonkeyPatch, status: int) -> None:
    [client] = sessions(monkeypatch, [response(status)])
    with pytest.raises(requests.HTTPError) as error:
        z.fetch_description(URL, delay=0)
    assert error.value.response is not None and error.value.response.status_code == status
    client.get.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize('location', ['https://external.test/job', '//evilziprecruiter.com/job',
                                     'https://ziprecruiter.com.external.test/job', 'http://www.ziprecruiter.com/job'])
@pytest.mark.parametrize('flight', [False, True])
def test_external_redirect_is_rejected_before_following(monkeypatch: MonkeyPatch, location: str, flight: bool) -> None:
    reply = response(text=f'0:{{"digest":"NEXT_REDIRECT;replace;{location};308;"}}', flight=True) if flight else response(308, location=location)
    [client] = sessions(monkeypatch, [reply])
    with pytest.raises(ValueError, match='external host or insecure'):
        z.fetch_description(URL, delay=0)
    client.get.assert_called_once()
    client.close.assert_called_once()


def test_prefers_complete_embedded_description_to_short_jsonld(monkeypatch: MonkeyPatch) -> None:
    data = json.dumps({'jobDetails': {'htmlFullDescription': '<p>Analyze samples and record QA results.</p>'}},
                      separators=(',', ':'))
    page = HTML + f'<script>self.__next_f.push([1,{json.dumps(data)}])</script>'
    sessions(monkeypatch, [response(text=page)])
    assert z.fetch_description(URL, delay=0) == ('Analyze samples and record QA results.', '')


def test_transport_error_recovers_with_a_fresh_session(monkeypatch: MonkeyPatch) -> None:
    clients = sessions(monkeypatch, [response(), response()])
    clients[0].get.side_effect = RequestException('network failure')
    assert z.fetch_description(URL, delay=0) == ('Analyze samples.', '')
    for client in clients:
        client.get.assert_called_once()
        client.close.assert_called_once()


def test_unresolved_flight_reference_stays_pending_after_retries(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = Store(tmp_path / 'jobs.db')
    store.upsert(Job(source='zip_recruiter', source_id='1', url=URL, title='Lab Technician',
                     company='Acme', city='Toronto', location='Toronto'), '2026-10-16T00:00:00+00:00')
    store.commit()
    stream = '0:{"jobDetails":{"htmlFullDescription":"$1"}}\n'
    clients = sessions(monkeypatch, [response(403), *[response(text=stream, flight=True)
                                                    for _ in range(z.MAX_REQUESTS - 1)]])
    assert _enrich(store, 'zip_recruiter', 25, lambda t: z.fetch_description(t.url, delay=0),
                   delay=0, stop_on_failure=False) == 0
    row = store.conn.execute('SELECT description, description_checked_at, description_attempted_at FROM jobs').fetchone()
    assert row['description'] == '' and row['description_checked_at'] is None
    assert row['description_attempted_at']
    assert len(store.missing_descriptions('zip_recruiter', 25)) == 1
    for client in clients:
        client.get.assert_called_once()
    store.close()


def test_valid_jsonld_survives_an_unresolved_embedded_reference(monkeypatch: MonkeyPatch) -> None:
    data = json.dumps({'jobDetails': {'htmlFullDescription': '$abcd'}}, separators=(',', ':'))
    page = HTML + f'<script>self.__next_f.push([1,{json.dumps(data)}])</script>'
    sessions(monkeypatch, [response(text=page)])
    assert z.fetch_description(URL, delay=0) == ('Analyze samples.', '')


@pytest.mark.parametrize('flight', [False, True])
def test_truncated_text_row_is_not_a_description(monkeypatch: MonkeyPatch, flight: bool) -> None:
    markup = '<p>Analyze samples and record results.</p>'
    truncated = '0:{"jobDetails":{"htmlFullDescription":"$1"}}\n1:T' + f'{len(markup.encode()):x},' + markup[:18]
    page = truncated if flight else f'<script>self.__next_f.push([1,{json.dumps(truncated)}])</script>'
    clients = sessions(monkeypatch, [response(text=page, flight=flight) for _ in range(z.MAX_REQUESTS)])
    with pytest.raises(ValueError, match='No ZipRecruiter description'):
        z.fetch_description(URL, delay=0)
    for client in clients:
        client.get.assert_called_once()


def test_valid_jsonld_survives_a_truncated_embedded_text_row(monkeypatch: MonkeyPatch) -> None:
    stream = '0:{"jobDetails":{"htmlFullDescription":"$1"}}\n1:T100,<p>partial'
    page = HTML + f'<script>self.__next_f.push([1,{json.dumps(stream)}])</script>'
    sessions(monkeypatch, [response(text=page)])
    assert z.fetch_description(URL, delay=0) == ('Analyze samples.', '')


def compact(data: object) -> str:
    return json.dumps(data, separators=(',', ':'))  # as Next.js serializes it


def flight_status(status: dict[str, object]) -> Mock:
    return response(text='0:' + compact({'jobDetails': {'status': status}}), flight=True)


@pytest.mark.parametrize('status,expected', [
    ({'postedAtUtc': '2026-10-21T01:17:08Z', 'deactivatedAtUtc': '2026-11-09T05:15:16Z'}, 'gone'),
    ({'isActive': True, 'postedAtUtc': '2026-09-08T23:00:00Z'}, 'alive'),
    ({'isActive': True, 'deactivatedAtUtc': '2026-11-09T05:15:16Z'}, 'unknown'),  # contradictory
])
def test_liveness_reads_posting_status(monkeypatch: MonkeyPatch, status: dict[str, object], expected: str) -> None:
    # The first HTML page has a description but no jobDetails: like descriptions, status retries with Flight.
    sessions(monkeypatch, [response(), flight_status(status)])
    monkeypatch.setattr('shigoto.sources.ziprecruiter_detail.time.sleep', lambda seconds: None)
    assert z.check_liveness(URL, 'zr-1') == expected


def test_html_status_is_read_without_flight(monkeypatch: MonkeyPatch) -> None:
    rows = '0:' + compact({'jobDetails': {'status': {'deactivatedAtUtc': '2026-11-09T05:15:16Z'}}})
    clients = sessions(monkeypatch, [response(text=f'<script>self.__next_f.push([1,{json.dumps(rows)}])</script>')])
    assert z.check_liveness(URL, 'zr-1') == 'gone'
    assert len(clients) == 1


@pytest.mark.parametrize('status,expected', [(404, 'gone'), (410, 'gone'), (403, 'unknown'), (500, 'unknown')])
def test_liveness_http_status(monkeypatch: MonkeyPatch, status: int, expected: str) -> None:
    sessions(monkeypatch, [response(status) for _ in range(z.MAX_REQUESTS)])
    monkeypatch.setattr('shigoto.sources.ziprecruiter_detail.time.sleep', lambda seconds: None)
    assert z.check_liveness(URL, 'zr-1') == expected


@pytest.mark.parametrize('status', [{}, {'isActive': False}, 'active'])
def test_unrecognized_status_is_unknown_after_retries(monkeypatch: MonkeyPatch, status: object) -> None:
    replies = [response(text='0:' + compact({'jobDetails': {'status': status}}), flight=True)
               for _ in range(z.MAX_REQUESTS)]
    clients = sessions(monkeypatch, replies)
    monkeypatch.setattr('shigoto.sources.ziprecruiter_detail.time.sleep', lambda seconds: None)
    assert z.check_liveness(URL, 'zr-1') == 'unknown'
    assert len(clients) == z.MAX_REQUESTS


def test_unrecognized_200_still_retries_descriptions_with_flight(monkeypatch: MonkeyPatch) -> None:
    stream = '0:' + compact({'jobDetails': {'htmlFullDescription': '<p>Plate samples.</p>'}})
    sessions(monkeypatch, [response(text='<html>Just a moment</html>'), response(text=stream, flight=True)])
    monkeypatch.setattr('shigoto.sources.ziprecruiter_detail.time.sleep', lambda seconds: None)
    assert z.fetch_description(URL) == ('Plate samples.', '')
