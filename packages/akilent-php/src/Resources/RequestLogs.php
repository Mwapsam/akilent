<?php

declare(strict_types=1);

namespace Akilent\Resources;

final class RequestLogs extends Base
{
    /** @param array<string,mixed> $filters */
    public function list(array $filters = []): array
    {
        return $this->t->request('GET', '/api/v1/request-logs', $filters ?: null);
    }

    public function retrieve(string $requestId): array
    {
        return $this->t->request('GET', "/api/v1/request-logs/{$requestId}");
    }
}
