<?php

declare(strict_types=1);

namespace Akilent\Tests;

use Akilent\Client;
use Akilent\ConflictError;
use Akilent\NotFoundError;
use Akilent\SignatureVerificationError;
use Akilent\Webhooks;
use PHPUnit\Framework\TestCase;

final class ClientTest extends TestCase
{
    /** @var array<int,array{method:string,url:string,headers:array<string,string>,body:?string}> */
    private array $calls = [];

    private function client(callable $responder): Client
    {
        $c = new Client('ak_test_abc', 'https://akilent.test');
        $c->transport->sender = function (string $method, string $url, array $headers, ?string $body) use ($responder): array {
            $this->calls[] = compact('method', 'url', 'headers', 'body');
            return $responder($method, $url, $headers, $body);
        };
        return $c;
    }

    public function testSendPostsWithAutoIdempotencyKey(): void
    {
        $c = $this->client(fn () => [202, json_encode(['id' => 1, 'public_id' => 'msg_1', 'status' => 'queued']), []]);
        $out = $c->messages->send(from: 'a@x.com', to: 'b@y.com', text: 'hi');

        $this->assertSame('msg_1', $out['public_id']);
        $this->assertSame('POST', $this->calls[0]['method']);
        $this->assertSame('https://akilent.test/api/v1/messages', $this->calls[0]['url']);
        $this->assertSame('Bearer ak_test_abc', $this->calls[0]['headers']['Authorization']);
        $this->assertArrayHasKey('Idempotency-Key', $this->calls[0]['headers']);
        $this->assertStringContainsString('"from":"a@x.com"', $this->calls[0]['body']);
    }

    public function testListBuildsQueryString(): void
    {
        $c = $this->client(fn () => [200, json_encode(['data' => [], 'total' => 0]), []]);
        $c->messages->list(['status' => 'delivered', 'limit' => 5]);
        $this->assertStringContainsString('status=delivered', $this->calls[0]['url']);
        $this->assertStringContainsString('limit=5', $this->calls[0]['url']);
    }

    public function testNotFoundRaisesTypedError(): void
    {
        $c = $this->client(fn () => [404, json_encode(['error' => ['code' => 'not_found', 'message' => 'nope', 'request_id' => 'req_9']]), []]);
        try {
            $c->messages->retrieve('msg_missing');
            $this->fail('expected NotFoundError');
        } catch (NotFoundError $e) {
            $this->assertSame(404, $e->statusCode);
            $this->assertSame('not_found', $e->errorCode);
            $this->assertSame('req_9', $e->requestId);
        }
    }

    public function testConflictOnIdempotencyReuse(): void
    {
        $c = $this->client(fn () => [409, json_encode(['error' => ['code' => 'idempotency_key_reuse', 'message' => 'reused']]), []]);
        $this->expectException(ConflictError::class);
        $c->messages->send(from: 'a@x.com', to: 'b@y.com', text: 'hi', idempotencyKey: 'k1');
    }

    public function testRetriesOn503ThenSucceeds(): void
    {
        $n = 0;
        $c = $this->client(function () use (&$n): array {
            $n++;
            return $n < 2 ? [503, '', []] : [200, json_encode(['data' => []]), []];
        });
        $c->messages->list();
        $this->assertSame(2, $n);
    }

    public function testIteratePagesThroughResults(): void
    {
        $pages = [
            [200, json_encode(['data' => [['id' => 'm1'], ['id' => 'm2']], 'total' => 3]), []],
            [200, json_encode(['data' => [['id' => 'm3']], 'total' => 3]), []],
        ];
        $i = 0;
        $c = $this->client(function () use (&$i, $pages): array {
            return $pages[$i++];
        });
        $ids = [];
        foreach ($c->messages->iterate([], 2) as $row) {
            $ids[] = $row['id'];
        }
        $this->assertSame(['m1', 'm2', 'm3'], $ids);
    }

    public function testWebhookVerifyRoundTrips(): void
    {
        $secret = 'whsec_test';
        $body = '{"event":"message.sent"}';
        $ts = time();
        $sig = "t={$ts},v1=" . hash_hmac('sha256', "{$ts}.{$body}", $secret);
        $this->assertTrue(Webhooks::verify($body, $sig, $secret));

        $this->expectException(SignatureVerificationError::class);
        Webhooks::verify($body . 'x', $sig, $secret);
    }
}
