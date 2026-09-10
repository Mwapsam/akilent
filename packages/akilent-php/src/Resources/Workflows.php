<?php

declare(strict_types=1);

namespace Akilent\Resources;

final class Workflows extends Base
{
    public function list(): array
    {
        return $this->t->request('GET', '/api/v1/workflows');
    }

    /** @param array<string,mixed> $body */
    public function create(array $body): array
    {
        return $this->t->request('POST', '/api/v1/workflows', null, $body);
    }

    public function publish(string $slug): array
    {
        return $this->t->request('POST', "/api/v1/workflows/{$slug}/publish", null, []);
    }

    /** @return array<int,array<string,mixed>> */
    public function templates(): array
    {
        return $this->t->request('GET', '/api/v1/workflows/templates')['data'] ?? [];
    }
}
