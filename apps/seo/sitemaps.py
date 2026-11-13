from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticPagesSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.8

    def items(self):
        return ["landing", "demo", "security", "help", "privacy", "terms", "docs"]

    def location(self, item):
        if item == "landing":
            return "/"
        if item == "docs":
            return "/docs/"
        return reverse(item)


class HelpArticlesSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.6

    def items(self):
        from apps.core import help as help_kb

        return help_kb.visible_articles()

    def location(self, article):
        return f"/help/{article.slug}/"


class DocsPagesSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.6

    def items(self):
        from apps.core import docs as docs_kb

        return [p for p in docs_kb.visible_pages() if p.slug != "index"]

    def location(self, page):
        return f"/docs/{page.slug}/"


class MarketingPagesSitemap(Sitemap):
    """Pricing, the product and integration pages and their hubs, and the use-case page: only
    those open right now (their views 404 the others)."""

    changefreq = "monthly"
    priority = 0.8

    def items(self):
        from apps.accounts import marketing_pages as mp
        from apps.accounts.views import marketing_channels

        ch = marketing_channels()
        pages = mp.visible(None, ch)
        items = [("/pricing/", None)]
        for kind in (mp.PRODUCT, mp.INTEGRATION):
            kind_pages = [p for p in pages if p.kind == kind]
            if kind_pages:
                items.append((mp.PREFIX[kind], max(p.updated for p in kind_pages)))
                items += [(p.url, p.updated) for p in kind_pages]
        if ch["whatsapp"] == "live":
            items.append((reverse("use-case", args=["missed-enquiries"]), None))
        return items

    def location(self, item):
        return item[0]

    def lastmod(self, item):
        return item[1]


class SEOPageSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.7

    def items(self):
        try:
            from .models import SEOPage

            return list(SEOPage.objects.filter(sitemap=True, noindex=False))
        except Exception:
            return []

    def location(self, obj):
        return obj.path

    def lastmod(self, obj):
        return obj.updated_at


SITEMAPS = {
    "static": StaticPagesSitemap,
    "help": HelpArticlesSitemap,
    "docs": DocsPagesSitemap,
    "marketing": MarketingPagesSitemap,
    "pages": SEOPageSitemap,
}
