import asyncio

import pytest
import pytest_asyncio
from packaging.version import Version

import testtools

pytestmark = [
    testtools.run_test_if_redis_ver('below', '4.2'),
]

aioredis = pytest.importorskip("aioredis")

import fakeredis.aioredis

aioredis2 = Version(aioredis.__version__) >= Version('2.0.0a1')
pytestmark.extend([
    pytest.mark.asyncio,
    pytest.mark.skipif(aioredis2, reason="Test is only applicable to aioredis 1.x"),
])


@pytest_asyncio.fixture(
    params=[
        pytest.param('fake', marks=pytest.mark.fake),
        pytest.param('real', marks=pytest.mark.real)
    ]
)
async def req_aioredis1(request):
    if request.param == 'fake':
        ret = await fakeredis.aioredis.create_redis_pool()
    else:
        if not request.getfixturevalue('is_redis_running'):
            pytest.skip('Redis is not running')
        ret = await aioredis.create_redis_pool('redis://localhost')
    await ret.flushall()

    yield ret

    await ret.flushall()
    ret.close()
    await ret.wait_closed()


@pytest_asyncio.fixture
async def conn(req_aioredis1):
    """A single connection, rather than a pool."""
    with await req_aioredis1 as conn:
        yield conn


async def test_ping(req_aioredis1):
    pong = await req_aioredis1.ping()
    assert pong == b'PONG'


async def test_types(req_aioredis1):
    await req_aioredis1.hmset_dict('hash', key1='value1', key2='value2', key3=123)
    result = await req_aioredis1.hgetall('hash', encoding='utf-8')
    assert result == {
        'key1': 'value1',
        'key2': 'value2',
        'key3': '123'
    }


async def test_transaction(req_aioredis1):
    tr = req_aioredis1.multi_exec()
    tr.set('key1', 'value1')
    tr.set('key2', 'value2')
    ok1, ok2 = await tr.execute()
    assert ok1
    assert ok2
    result = await req_aioredis1.get('key1')
    assert result == b'value1'


async def test_transaction_fail(req_aioredis1, conn):
    # ensure that the WATCH applies to the same connection as the MULTI/EXEC.
    await req_aioredis1.set('foo', '1')
    await conn.watch('foo')
    await conn.set('foo', '2')  # Different connection
    tr = conn.multi_exec()
    tr.get('foo')
    with pytest.raises(aioredis.MultiExecError):
        await tr.execute()


async def test_pubsub(req_aioredis1, event_loop):
    ch, = await req_aioredis1.subscribe('channel')
    queue = asyncio.Queue()

    async def reader(channel):
        async for message in ch.iter():
            queue.put_nowait(message)

    task = event_loop.create_task(reader(ch))
    await req_aioredis1.publish('channel', 'message1')
    await req_aioredis1.publish('channel', 'message2')
    result1 = await queue.get()
    result2 = await queue.get()
    assert result1 == b'message1'
    assert result2 == b'message2'
    ch.close()
    await task


async def test_blocking_ready(req_aioredis1, conn):
    """Blocking command which does not need to block."""
    await req_aioredis1.rpush('list', 'x')
    result = await conn.blpop('list', timeout=1)
    assert result == [b'list', b'x']


@pytest.mark.slow
async def test_blocking_timeout(conn):
    """Blocking command that times out without completing."""
    result = await conn.blpop('missing', timeout=1)
    assert result is None


@pytest.mark.slow
async def test_blocking_unblock(req_aioredis1, conn, event_loop):
    """Blocking command that gets unblocked after some time."""

    async def unblock():
        await asyncio.sleep(0.1)
        await req_aioredis1.rpush('list', 'y')

    task = event_loop.create_task(unblock())
    result = await conn.blpop('list', timeout=1)
    assert result == [b'list', b'y']
    await task


@pytest.mark.slow
async def test_blocking_pipeline(conn):
    """Blocking command with another command issued behind it."""
    await conn.set('foo', 'bar')
    fut = asyncio.ensure_future(conn.blpop('list', timeout=1))
    assert (await conn.get('foo')) == b'bar'
    assert (await fut) is None


async def test_wrongtype_error(req_aioredis1):
    await req_aioredis1.set('foo', 'bar')
    with pytest.raises(aioredis.ReplyError, match='^WRONGTYPE'):
        await req_aioredis1.rpush('foo', 'baz')


async def test_syntax_error(req_aioredis1):
    with pytest.raises(aioredis.ReplyError,
                       match="^ERR wrong number of arguments for 'get' command$"):
        await req_aioredis1.execute('get')


async def test_no_script_error(req_aioredis1):
    with pytest.raises(aioredis.ReplyError, match='^NOSCRIPT '):
        await req_aioredis1.evalsha('0123456789abcdef0123456789abcdef')


@testtools.run_test_if_lupa
async def test_failed_script_error(req_aioredis1):
    await req_aioredis1.set('foo', 'bar')
    with pytest.raises(aioredis.ReplyError, match='^ERR Error running script'):
        await req_aioredis1.eval('return redis.call("ZCOUNT", KEYS[1])', ['foo'])
