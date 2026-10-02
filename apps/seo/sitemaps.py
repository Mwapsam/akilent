from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticPagesSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.8

    def items(self):
        return ["landing", "help", "privacy", "terms", "docs"]

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

        return list(help_kb.ARTICLES)

    def location(self, article):
        return f"/help/{article.slug}/"


class DocsPagesSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.6

    def items(self):
        from apps.core import docs as docs_kb

        return [p for p in docs_kb.PAGES if p.slug != "index"]

    def location(self, page):
        return f"/docs/{page.slug}/"


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
    "pages": SEOPageSitemap,
}
