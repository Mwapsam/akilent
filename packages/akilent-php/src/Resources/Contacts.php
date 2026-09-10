<?php

declare(strict_types=1);

namespace Akilent\Resources;

final class Contacts extends Base
{
    /** @param array<string,mixed> $filters */
    public function list(array $filters = []): array
    {
        return $this->t->request('GET', '/api/v1/contacts', $filters ?: null);
    }

    /** @param array<string,mixed> $body */
    public function upsert(array $body): array
    {
        return $this->t->request('POST', '/api/v1/contacts', null, $body);
    }

    public function retrieve(string $id): array
    {
        return $this->t->request('GET', "/api/v1/contacts/{$id}");
    }

    /** @param array<string,mixed> $body */
    public function update(string $id, array $body): array
    {
        return $this->t->request('PATCH', "/api/v1/contacts/{$id}", null, $body);
    }

    public function delete(string $id): void
    {
        $this->t->request('DELETE', "/api/v1/contacts/{$id}");
    }
}
