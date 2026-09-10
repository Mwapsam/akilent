<?php

declare(strict_types=1);

namespace Akilent\Resources;

final class Events extends Base
{
    /** @param array<string,mixed> $body */
    public function ingest(array $body): array
    {
        return $this->t->request('POST', '/api/v1/events', null, $body);
    }

    /** @param array<string,mixed> $filters */
    public function list(array $filters = []): array
    {
        return $this->t->request('GET', '/api/v1/events', $filters ?: null);
    }
}
