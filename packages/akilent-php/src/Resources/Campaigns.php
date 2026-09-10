<?php

declare(strict_types=1);

namespace Akilent\Resources;

final class Campaigns extends Base
{
    /** @param array<string,mixed> $body */
    public function create(array $body): array
    {
        return $this->t->request('POST', '/api/v1/campaigns', null, $body);
    }

    public function retrieve(int $id): array
    {
        return $this->t->request('GET', "/api/v1/campaigns/{$id}");
    }

    /** @return array<int,array<string,mixed>> */
    public function versions(int $id): array
    {
        return $this->t->request('GET', "/api/v1/campaigns/{$id}/versions")['data'] ?? [];
    }
}
