<?php

declare(strict_types=1);

namespace Akilent\Resources;

final class Templates extends Base
{
    public function list(): array
    {
        return $this->t->request('GET', '/api/v1/templates');
    }

    /** @param array<string,mixed> $body */
    public function create(array $body): array
    {
        return $this->t->request('POST', '/api/v1/templates', null, $body);
    }

    public function retrieve(string $slug): array
    {
        return $this->t->request('GET', "/api/v1/templates/{$slug}");
    }

    /** @param array<string,mixed> $body */
    public function update(string $slug, array $body): array
    {
        return $this->t->request('PATCH', "/api/v1/templates/{$slug}", null, $body);
    }

    public function delete(string $slug): void
    {
        $this->t->request('DELETE', "/api/v1/templates/{$slug}");
    }

    /** @param array<string,mixed>|null $variables */
    public function preview(string $slug, ?array $variables = null, ?string $locale = null): array
    {
        $body = ['variables' => $variables ?? []];
        if ($locale !== null) {
            $body['locale'] = $locale;
        }
        return $this->t->request('POST', "/api/v1/templates/{$slug}/preview", null, $body);
    }

    /** @return array<int,array<string,mixed>> */
    public function versions(string $slug): array
    {
        return $this->t->request('GET', "/api/v1/templates/{$slug}/versions")['data'] ?? [];
    }

    public function activateVersion(string $slug, int $number): array
    {
        return $this->t->request('POST', "/api/v1/templates/{$slug}/versions/{$number}/activate", null, []);
    }

    /** @return array<int,array<string,mixed>> */
    public function locales(string $slug): array
    {
        return $this->t->request('GET', "/api/v1/templates/{$slug}/locales")['data'] ?? [];
    }

    /** @param array<string,mixed> $body */
    public function upsertLocale(string $slug, string $locale, array $body): array
    {
        return $this->t->request('PUT', "/api/v1/templates/{$slug}/locales/{$locale}", null, $body);
    }
}
