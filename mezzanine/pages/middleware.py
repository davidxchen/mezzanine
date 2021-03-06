from __future__ import unicode_literals

from django.contrib.auth import REDIRECT_FIELD_NAME
from django.core.exceptions import ImproperlyConfigured, MiddlewareNotUsed
from django.http import HttpResponse, Http404
from django.shortcuts import redirect
from django.utils.http import urlquote

from mezzanine.conf import settings
from mezzanine.pages import page_processors
from mezzanine.pages.models import Page
from mezzanine.pages.views import page as page_view
from mezzanine.utils.importing import import_dotted_path
from mezzanine.utils.urls import path_to_slug


class PageMiddleware(object):
    """
    Adds a page to the template context for the current response.

    If no page matches the URL, and the view function is not the
    fall-back page view, we try and find the page with the deepest
    URL that matches within the current URL, as in this situation,
    the app's urlpattern is considered to sit "under" a given page,
    for example the blog page will be used when individual blog
    posts are viewed. We want the page for things like breadcrumb
    nav, and page processors, but most importantly so the page's
    ``login_required`` flag can be honoured.

    If a page is matched, and the fall-back page view is called,
    we add the page to the ``extra_context`` arg of the page view,
    which it can then use to choose which template to use.

    In either case, we add the page to the response's template
    context, so that the current page is always available.
    """

    def __init__(self):
        if "mezzanine.pages" not in settings.INSTALLED_APPS:
            raise MiddlewareNotUsed

    @classmethod
    def installed(cls):
        """
        Used in ``mezzanine.pages.views.page`` to ensure
        ``PageMiddleware`` or a subclass has been installed. We cache
        the result on the ``PageMiddleware._installed`` to only run
        this once. Short path is to just check for the dotted path to
        ``PageMiddleware`` in ``MIDDLEWARE_CLASSES`` - if not found,
        we need to load each middleware class to match a subclass.
        """
        try:
            return cls._installed
        except AttributeError:
            name = "mezzanine.pages.middleware.PageMiddleware"
            installed = name in settings.MIDDLEWARE_CLASSES
            if not installed:
                for name in settings.MIDDLEWARE_CLASSES:
                    if issubclass(import_dotted_path(name), cls):
                        installed = True
                        break
            setattr(cls, "_installed", installed)
            return installed

    def process_view(self, request, view_func, view_args, view_kwargs):
        """
        Per-request mechanics for the current page object.
        """

        cp = "mezzanine.pages.context_processors.page"
        if cp not in settings.TEMPLATE_CONTEXT_PROCESSORS:
            raise ImproperlyConfigured("%s is missing from "
                "settings.TEMPLATE_CONTEXT_PROCESSORS" % cp)

        # Load the closest matching page by slug, and assign it to the
        # request object. If none found, skip all further processing.
        slug = path_to_slug(request.path_info)
        pages = Page.objects.with_ascendants_for_slug(slug,
                        for_user=request.user, include_login_required=True)
        if pages:
            page = pages[0]
            setattr(request, "page", page)
        else:
            return

        # Handle ``page.login_required``.
        if page.login_required and not request.user.is_authenticated():
            path = urlquote(request.get_full_path())
            bits = (settings.LOGIN_URL, REDIRECT_FIELD_NAME, path)
            return redirect("%s?%s=%s" % bits)

        # Here we do a wacky check with non-page views and 404s.
        # Basically if the view function isn't the page view and
        # raises a 404, but also matches an exact page slug, we then
        # forget about the non-page view, and run the page view
        # with the correct args.
        # This check allows us to set up pages with URLs that also
        # match non-page urlpatterns, for example a page could be
        # created with the URL /blog/about/, which would match the
        # blog urlpattern, and assuming there wasn't a blog post
        # with the slug "about", would raise a 404.
        try:
            response = view_func(request, *view_args, **view_kwargs)
        except Http404:
            if (page.slug == slug and view_func != page_view and
                    page.content_model != 'link'):
                # Matched a non-page urlpattern, but got a 404
                # for a URL that matches a valid page slug, so
                # use the page view.
                response = page_view(request, slug, **view_kwargs)
            else:
                raise

        # Run page processors.
        model_processors = page_processors.processors[page.content_model]
        slug_processors = page_processors.processors["slug:%s" % page.slug]
        for (processor, exact_page) in slug_processors + model_processors:
            if exact_page and not page.is_current:
                continue
            processor_response = processor(request, page)
            if isinstance(processor_response, HttpResponse):
                return processor_response
            elif processor_response:
                try:
                    for k in processor_response:
                        if k not in response.context_data:
                            response.context_data[k] = processor_response[k]
                except (TypeError, ValueError):
                    name = "%s.%s" % (processor.__module__, processor.__name__)
                    error = ("The page processor %s returned %s but must "
                             "return HttpResponse or dict." %
                             (name, type(processor_response)))
                    raise ValueError(error)

        return response
