import re
import math
import logging
import datetime
import pysolr
from urllib.parse import urlencode, unquote

from django import urls
from django.core.paginator import Paginator, Page
from django.db import connection, reset_queries
from django.http import QueryDict
from django.conf import settings

from core import models
from core.utils.utils import fulltext_range
from core.utils import utils
from core.title_loader import _normal_lccn

_log = logging.getLogger(__name__)

PROX_DISTANCE_DEFAULT = 5

ESCAPE_CHARS_RE = re.compile(r'(?<!\\)(?P<char>[&|+\-!(){}[\]^"~*?:])')

def conn():
    return pysolr.Solr(settings.SOLR)

def page_count():
    return len(conn().search(q='type:page', fl='id'))

def _solr_escape(value):
    """
    Escape un-escaped special characters and return escaped value.
    >>> _solr_escape(r'foo+') == r'foo\+'
    True
    >>> _solr_escape(r'foo\+') == r'foo\+'
    True
    >>> _solr_escape(r'foo\\+') == r'foo\\+'
    True
    """
    return ESCAPE_CHARS_RE.sub(r'\\\g<char>', value)

def _sort_facets_asc(solr_facets, field):
    items = list(solr_facets.get('facet_fields')[field].items())
    return sorted(items, key = lambda item: int(item[1]), reverse = True)

def title_count():
    return len(conn().search(q='type:title', fl='id'))

class SolrPaginator(Paginator):
    """
    SolrPaginator takes a QueryDict object, builds and executes a solr query for
    newspaper pages, and returns a paginator for the search results for use in
    a HTML form.
    """

    def __init__(self, query):
        self.query = query.copy()

        # remove words from query as it's not part of the solr query.
        if 'words' in self.query:
            del self.query['words']

        self._q, self.facet_params = page_search(self.query)

        try:
            self._cur_page = int(self.query.get('page'))
        except:
            self._cur_page = 1  # _cur_page is 1-based

        try:
            self._cur_index = int(self.query.get('index'))
        except:
            self._cur_index = 0

        try:
            rows = int(self.query.get('rows'))
        except:
            rows = 10

        # set up some bits that the Paginator expects to be able to use
        Paginator.__init__(self, None, per_page=rows, orphans=0)

        self.overall_index = (self._cur_page - 1) * self.per_page + self._cur_index

        self._ocr_list = ['ocr',]
        self._ocr_list.extend(['ocr_%s' % l for l in settings.SOLR_LANGUAGES])

    def _get_count(self):
        "Returns the total number of objects, across all pages."
        if not hasattr(self, '_count'):
            self._count = len(conn().search(self._q, fl='id'))
        return self._count
    count = property(_get_count)

    def highlight_url(self, url, words):
        q = QueryDict(None, True)
        if words:
            q["words"] = " ".join(words)
            return url + "#" + q.urlencode()
        else:
            return url

    def pagination_url(self, url, words, page, index):
        q = self.query.copy()
        q["words"] = " ".join(words)
        q["page"] = page
        q["index"] = index
        return url + "#" + q.urlencode()

    def _get_previous(self):
        previous_overall_index = self.overall_index - 1
        if previous_overall_index >= 0:
            p_page = previous_overall_index // self.per_page + 1
            p_index = previous_overall_index % self.per_page
            o = self.page(p_page).object_list[p_index]
            q = self.query.copy()
            return self.pagination_url(o.url, o.words, p_page, p_index)
        else:
            return None
    previous_result = property(_get_previous)

    def _get_next(self):
        next_overall_index = self.overall_index + 1
        if next_overall_index < self.count:
            n_page = next_overall_index // self.per_page + 1
            n_index = next_overall_index % self.per_page
            o = self.page(n_page).object_list[n_index]
            return self.pagination_url(o.url, o.words, n_page, n_index)
        else:
            return None
    next_result = property(_get_next)

    def page(self, number):
        """
        Override the page method in Paginator since Solr has already
        paginated stuff for us.
        """

        number = self.validate_number(number)

        # figure out the solr query and execute it
        solr = SolrConnection(settings.SOLR) # TODO: maybe keep connection around?
        start = self.per_page * (number - 1)
        params = {"hl.snippets": 100, # TODO: make this unlimited
            "hl.requireFieldMatch": 'true', # limits highlighting slop
            "hl.maxAnalyzedChars": '102400', # increased from default 51200
            }
        params.update(self.facet_params)
        sort_field, sort_order = _get_sort(self.query.get('sort'), in_pages=True)
        solr_response = solr.query(self._q,
                                   fields=['id', 'title', 'date', 'month', 'day',
                                           'sequence', 'edition_label', 
                                           'section_label'],
                                   highlight=self._ocr_list,
                                   rows=self.per_page,
                                   sort=sort_field,
                                   sort_order=sort_order,
                                   start=start,
                                   **params)
        solr_facets = solr_response.facet_counts
        # sort states by number of hits per state (desc)
        facets = {
            'city': _sort_facets_asc(solr_facets, 'city'),
            'county': _sort_facets_asc(solr_facets, 'county'),
            'frequency': _sort_facets_asc(solr_facets, 'frequency'),
            'language': _sort_facets_asc(solr_facets, 'language'),
            'state': _sort_facets_asc(solr_facets, 'state'),
        }
        # sort by year (desc)
        facets['year'] = sorted(list(solr_facets['facet_ranges']['year']['counts'].items()),
                                key = lambda k: k[0], reverse = True)
        facet_gap = self.facet_params['f_year_facet_range_gap']
        if facet_gap > 1:
            facets['year'] = [('%s-%d' % (y[0], int(y[0])+facet_gap-1), y[1]) 
                              for y in facets['year']]
        pages = []
        for result in solr_response.results:
            page = models.Page.lookup(result['id'])
            if not page:
                continue
            words = set()
            coords = solr_response.highlighting[result['id']]
            for ocr in self._ocr_list:
                for s in coords.get(ocr) or []:
                    words.update(find_words(s))
            page.words = sorted(words, key=lambda v: v.lower())

            page.highlight_url = self.highlight_url(page.url, page.words)
            pages.append(page)

        solr_page = Page(pages, number, self)
        solr_page.facets = facets
        return solr_page

    def pages(self):
        """
        pages creates a list of two element tuples (page_num, url)
        rather than displaying all the pages for large result sets
        it provides windows into the results like digg:

           1 2 3 ... 8 9 10 11 12 13 14 ... 87 88 89

        """
        pages = []

        # build up the segments
        before = []
        middle = []
        end = []
        for p in self.page_range:
            if p <= 3:
                before.append(p)
            elif self._num_pages - p <= 3:
                end.append(p)
            elif abs(p - self._cur_page) < 5:
                middle.append(p)

        # create the list with '...' where the sequence breaks
        last = None
        q = self.query.copy()
        for p in before + middle + end:
            if last and p - last > 1:
                pages.append(['...', None])
            else:
                q['page'] = p
                pages.append([p, urlencode(q)])
            last = p

        return pages

    def englishify(self):
        """
        Returns some pseudo english text describing the query.
        """
        d = self.query
        parts = []
        if d.get('ortext', None):
            parts.append(' OR '.join(d['ortext'].split(' ')))
        if d.get('andtext', None):
            parts.append(' AND '.join(d['andtext'].split(' ')))
        if d.get('phrasetext', None):
            parts.append('the phrase "%s"' % d['phrasetext'])
        if d.get('proxtext', None):
            proxdistance = d.get('proxdistance', PROX_DISTANCE_DEFAULT)
            parts.append(d['proxtext'])
        return parts

class SolrTitlesPaginator(Paginator):
    """
    SolrTitlesPaginator takes a QueryDict object, builds and executes a solr
    query for newspaper titles, and returns a paginator for the search results
    for use in a HTML form.
    """

    def __init__(self, query):
        self.query = query.copy()
        q, fields, sort_field, sort_order, facets = get_solr_request_params_from_query(self.query)
        year_facets = facets.pop('year_facets')
        try:
            page = int(self.query.get('page'))
        except:
            page = 1

        try:
            rows = int(self.query.get('rows'))
        except:
            rows = 50
        start = rows * (page - 1)
        query = q
        # execute query
        solr_response = execute_solr_query(q, fields, sort_field, sort_order, rows, start, facets)

        # convert the solr documents to Title models
        # could use solr doc instead of going to db, if performance requires it
        results = get_titles_from_solr_documents(solr_response)

        # set up some bits that the Paginator expects to be able to use
        Paginator.__init__(self, results, per_page=rows, orphans=0)
        self._count = len(solr_response)
        self._num_pages = None
        self._cur_page = page
        self.state_facets = _sort_facets_asc(solr_response.facet_counts, 'state')
        self.county_facets = _sort_facets_asc(solr_response.facet_counts, 'county')
        self.year_facets = year_facets

    def page(self, number):
        """
        Override the page method in Paginator since Solr has already
        paginated stuff for us.
        """
        number = self.validate_number(number)
        return Page(self.object_list, number, self)


def get_titles_from_solr_documents(solr_response):
    """
    solr_response: search result returned from SOLR in response to
    title search.

    This function turns SOLR documents into openoni.models.Title 
    instances 
    """
    lccns = [d['lccn'] for d in solr_response.results]
    results = []
    for lccn in lccns:
        try:
            title = models.Title.objects.get(lccn=lccn)
            results.append(title)
        except models.Title.DoesNotExist as e:
            pass # TODO: log exception
    return results


def get_solr_request_params_from_query(query):
    q, facets = title_search(query)
    fields = ['id', 'title', 'date', 'sequence', 'edition_label', 'section_label']
    sort_field, sort_order = _get_sort(query.get('sort'))
    return q, fields, sort_field, sort_order, facets
 

def execute_solr_query(query, fields, sort, sort_order, rows, start, facets):
    # default arg_separator - underscore wont work if fields to facet on 
    # themselves have underscore in them
    solr = SolrConnection(settings.SOLR) # TODO: maybe keep connection around?
    solr_response = solr.query(query, 
                               fields=['lccn', 'title',
                                       'edition',
                                       'place_of_publication',
                                       'start_year', 'end_year',
                                       'language'],
                               rows=rows,
                               sort=sort,
                               sort_order=sort_order,
                               start=start, **facets)
    return solr_response

def title_search(d):
    """
    Pass in form data for a given title search, and get back
    a corresponding solr query.
    """
    q = ["+type:title"]
    if d.get('state'):
        q.append('+state:"%s"' % d['state'])
    if d.get('county'):
        q.append('+county:"%s"' % d['county'])
    if d.get('city'):
        q.append('+city:"%s"' % d['city'])
    for term in d.get('terms', '').replace('"', '').split():
        q.append('+(title:"%s" OR essay:"%s" OR note:"%s" OR edition:"%s" OR place_of_publication:"%s" OR url:"%s" OR publisher:"%s")' % (term, term, term, term, term, term, term))
    if d.get('frequency'):
        q.append('+frequency:"%s"' % d['frequency'])
    if d.get('language'):
        q.append('+language:"%s"' % d['language'])
    if d.get('ethnicity'):
        q.append('+' + _expand_ethnicity(d['ethnicity']))
    if d.get('labor'):
        q.append('+subject:"%s"' % d['labor'])
    year1 = d.get('year1', None)
    if not year1:
        year1 = '1690'
    year2 = d.get('year2', None)
    if not year2:
        year2 = '2009'
    # don't add the start_year restriction if it's the lowest allowed year
    if year1 != '1690':
        q.append('+end_year:[%s TO 9999]' % year1)
    # don't add the end_year restriction if it's the max allowed year
    # particularly important for end_years that are coded as 'current'
    if year2 != '2009':
        q.append('+start_year: [0 TO %s]' % year2)
    if d.get('lccn'):
        q.append('+lccn:"%s"' % _normal_lccn(d['lccn']))
    if d.get('material_type'):
        q.append('+holding_type:"%s"' % d['material_type'])
    q = ' '.join(q)
    # keep the gap 10 for year range 100, 20 for year range 200 and so on
    range_gap = int(math.ceil((int(year2) - int(year1)) / 100.0)) * 5
    year_facets = list(range(int(year1), int(year2), range_gap or 1))
    facets = {'facet': 'true', 'facet_field': ['state', 'county'], 
              'facet_mincount': 1, 'year_facets': year_facets}
    return q, facets

def page_search(d):
    """
    Pass in form data for a given page search, and get back
    a corresponding solr query.
    """
    q = ['+type:page']

    simple_fields = ['city', 'county', 'frequency', 
                     'state', 'lccn'
                    ]

    for field in simple_fields:
        if d.get(field, None):
            q.append(query_join(d.getlist(field), field))

    ocrs = ['ocr_%s' % l for l in settings.SOLR_LANGUAGES]

    lang_req = d.get('language', None)
    language = models.Language.objects.get(name=lang_req) if lang_req else None
    lang = language.code if language else None
    if language:
        q.append('+language:%s' % language.name)
    ocr_lang = 'ocr_' + lang if lang else 'ocr'
    if d.get('ortext', None):
        q.append('+((' + query_join(_solr_escape(d['ortext']).split(' '), "ocr"))
        if lang:
            q.append(' AND ' + query_join(_solr_escape(d['ortext']).split(' '), ocr_lang))
            q.append(') OR ' + query_join(_solr_escape(d['ortext']).split(' '), ocr_lang))
        else:
            q.append(')')
            for ocr  in ocrs:
                q.append('OR ' + query_join(_solr_escape(d['ortext']).split(' '), ocr))
        q.append(')')
    if d.get('andtext', None):
        q.append('+((' + query_join(_solr_escape(d['andtext']).split(' '), "ocr", and_clause=True))
        if lang:
            q.append('AND ' + query_join(_solr_escape(d['andtext']).split(' '), ocr_lang, and_clause=True))
            q.append(') OR ' + query_join(_solr_escape(d['andtext']).split(' '), ocr_lang, and_clause=True))
        else:
            q.append(')')
            for ocr in ocrs:
                q.append('OR ' + query_join(_solr_escape(d['andtext']).split(' '), ocr, and_clause=True))
        q.append(')')
    if d.get('phrasetext', None):
        phrase = _solr_escape(d['phrasetext'])
        q.append('+((' + 'ocr' + ':"%s"^10000' % (phrase))
        if lang:
            q.append('AND ocr_' + lang + ':"%s"' % (phrase))
            q.append(') OR ocr_' + lang + ':"%s"' % (phrase))
        else:
            q.append(')')
            for ocr in ocrs:
                q.append('OR ' + ocr + ':"%s"' % (phrase))
        q.append(')')

    if d.get('proxtext', None):
        distance = d.get('proxdistance', PROX_DISTANCE_DEFAULT)
        prox = _solr_escape(d['proxtext'])
        q.append('+((' + 'ocr' + ':("%s"~%s)^10000' % (prox, distance))
        if lang:
            q.append('AND ocr_' + lang + ':"%s"~%s' % (prox, distance))
            q.append(') OR ocr_' + lang + ':"%s"~%s' % (prox, distance))
        else:
            q.append(')')
            for ocr in ocrs:
                q.append('OR ' + ocr + ':"%s"~%s' % (prox, distance))
        q.append(')')
    if d.get('sequence', None):
        q.append('+sequence:"%s"' % d['sequence'])
    if d.get('issue_date', None):
        q.append('+month:%d +day:%d' % (int(d['date_month']), int(d['date_day'])))

    # yearRange supercedes date1 and date2

    year1, year2 = None, None

    year_range = d.get('yearRange', None)
    if year_range:
        split = year_range.split("-")
        if len(split) == 2:
            year1 = int(split[0])
            year2 = int(split[1])
        else:
            year1 = int(split[0])
            year2 = int(split[0])
        q.append('+year:[%d TO %d]' % (year1, year2))
    else:
        date_boundaries = fulltext_range()
        date1 = d.get('date1', None)
        date2 = d.get('date2', None)
        if date1 or date2:
            # do NOT apply year min / max to solr query
            # do apply it to facets since they require a specific begin / end year
            d1 = _solrize_date(str(date1), 'start')
            d2 = _solrize_date(str(date2), 'end')

            q.append('+date:[%s TO %s]' % (d1, d2))
            year1 = date_boundaries[0] if d1 == "*" else int(str(d1)[:4])
            year2 = date_boundaries[1] if d2 == "*" else int(str(d2)[:4])
        else:
            # do not pass any query parameters to solr if no date requested
            # but do set the year range for faceting purposes
            year1 = date_boundaries[0]
            year2 = date_boundaries[1]

    # choose a facet range gap such that the number of date ranges returned
    # is <= 10. These would be used to populate a select dropdown on search
    # results page.
    gap = max(1, int(math.ceil((year2 - year1)//10)))

    # increment year range end by 1 to be inclusive
    facet_params = {'facet': 'true','facet_field': [
                    'city',
                    'county',
                    'frequency',
                    'language',
                    'state', 
                    ],
                    'facet_range':'year',
                    'f_year_facet_range_start': year1,
                    'f_year_facet_range_end': year2+1,
                    'f_year_facet_range_gap': gap,
                    'facet_mincount': 1
                    }
    return ' '.join(q), facet_params

def query_join(values, field, and_clause=False):
    """
    helper to create a chunk of a lucene query, based on
    some value(s) extracted from form data
    """

    # might be single value or a list of values
    if not isinstance(values, list):
        values = [values]

    # escape solr chars
    values = [_solr_escape(v) for v in values]

    # quote values
    values = ['"%s"' % v for v in values]

    # add + to the beginning of each value if we are doing an AND clause
    if and_clause:
        values = ["+%s" % v for v in values]

    # return the lucene query chunk
    if field.startswith("ocr"):
        if field == "ocr":
            return "%s:(%s)^10000" % (field, ' '.join(values))
        else:
            return "%s:(%s)" % (field, ' '.join(values))
    else:
        return "+%s:(%s)" % (field, ' '.join(values))


def find_words(s):
    ems = re.findall('<em>.+?</em>', s)
    words = [em[4:-5] for em in ems] # strip <em> and </em>
    return words


def index_titles(since=None):
    """index all the titles and holdings that are modeled in the database
    if you pass in a datetime object as the since parameter only title
    records that have been created since that time will be indexed.
    """
    cursor = connection.cursor()
    if since:
        cursor.execute("SELECT lccn FROM core_title WHERE created >= '%s'" % since)
    else:
        conn().delete(q='type:title')
        cursor.execute("SELECT lccn FROM core_title")

    count = 0
    while True:
        row = cursor.fetchone()
        if row == None:
            break
        title = models.Title.objects.get(lccn=row[0])
        index_title(title)
        count += 1
        if count % 100 == 0:
            _log.info("indexed %s titles" % count)
            reset_queries()
            conn().commit()
    conn().commit()

def index_title(title):
    _log.info("indexing title: lccn=%s" % title.lccn)
    try:
        conn().add(title.solr_doc)
    except Exception as e:
        _log.exception(e)

def delete_title(title):
    r = conn().delete(q='+type:title +id:%s' % title.solr_doc['id'])
    _log.info("deleted title %s from the index" % title)

def index_pages():
    """index all the pages that are modeled in the database
    """
    _log = logging.getLogger(__name__)
    cursor = connection.cursor()
    cursor.execute("SELECT id FROM core_page WHERE ocr_filename IS NOT NULL AND ocr_filename <> ''")
    count = 0
    while True:
        row = cursor.fetchone()
        if row == None:
            break
        page = models.Page.objects.get(id=row[0])
        _log.info("[%s] indexing page: %s" % (count, page.url))
        conn().add(page.solr_doc)
        count += 1
        if count % 100 == 0:
            reset_queries()
    conn().commit()

def word_matches_for_page(page_id, words):
    """
    Gets a list of pre-analyzed words for a list of words on a particular
    page. So if you pass in 'manufacturer' you can get back a list like
    ['Manufacturer', 'manufacturers', 'MANUFACTURER'] etc ...
    """
    solr = SolrConnection(settings.SOLR)

    # Make sure page_id is of type str, else the following string
    # operation may result in a UnicodeDecodeError. For example, see
    # ticket #493
    if not isinstance(page_id, str):
        page_id = str(page_id)

    ocr_list = ['ocr',]
    ocr_list.extend(['ocr_%s' % l for l in settings.SOLR_LANGUAGES])
    ocrs = ' OR '.join([query_join(words, o) for o in ocr_list])
    q = 'id:%s AND (%s)' % (page_id, ocrs)
    params = {"hl.snippets": 100, "hl.requireFieldMatch": 'true', "hl.maxAnalyzedChars": '102400'}
    response = solr.query(q, fields=['id'], highlight=ocr_list, **params)

    if page_id not in response.highlighting:
        return []

    words = set()
    for ocr in ocr_list:
        if ocr in response.highlighting[page_id]:
            for context in response.highlighting[page_id][ocr]:
                words.update(find_words(context))
    return list(words)

def _get_sort(sort, in_pages=False):
    sort_field = sort_order = None
    if sort == 'state':
        sort_field = 'country' # odd artifact of Title model
        sort_order = 'asc'
    elif sort == 'title':
        # important to sort on title_facet since it's the original
        # string, and not the analyzed title
        sort_field = 'title_normal'
        sort_order = 'asc'
    # sort by the full issue date if we searching pages
    elif sort == 'date' and in_pages:
        sort_field = 'date'
        sort_order = 'asc'
    elif sort == 'date':
        sort_field = 'start_year'
        sort_order = 'asc'
    return sort_field, sort_order

def _expand_ethnicity(e):
    """
    takes an ethnicity string, expands it out the query using the
    the EthnicitySynonym models, and returns a chunk of a lucene query
    """
    parts = ['subject:"%s"' % e]
    ethnicity = models.Ethnicity.objects.get(name=e)
    for s in ethnicity.synonyms.all():
        parts.append('subject:"%s"' % s.synonym)
    q = ' OR '.join(parts)
    return "(" + q + ")"

def _solrize_date(date, date_type=''):
    """
    Takes a date string like 2018/01/01 and returns an
    integer suitable for querying the date field in a solr document.
    """
    solr_date = "*"
    if date:
        date = date.strip()

        start_year, end_year = fulltext_range()
        if date_type == 'start' and date == str(start_year) +'-01-01':
            return '*'
        elif date_type == 'end' and date == str(end_year) +'-12-31':
            return '*'

        # 1900-01-01 -> 19000101
        match = re.match(r'(\d{4})-(\d{2})-(\d{2})', date)
        if match:
            y, m, d = match.groups()
            if y and m and d:
                solr_date = y+m+d
    return solr_date
