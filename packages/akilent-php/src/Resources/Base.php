<?php

declare(strict_types=1);

namespace Akilent\Resources;

use Akilent\Transport;

abstract class Base
{
    public function __construct(protected Transport $t)
    {
    }
}
