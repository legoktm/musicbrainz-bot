#!/usr/bin/python

import sys
import os
import re
import discogs_client as discogs
import sqlalchemy
from amazonproduct.contrib.retry import RetryAPI
import time
from editing import MusicBrainzClient
import socket
from utils import out, colored_out, bcolors
import config as cfg

engine = sqlalchemy.create_engine(cfg.MB_DB)
db = engine.connect()
db.execute("SET search_path TO musicbrainz")

mb = MusicBrainzClient(cfg.MB_USERNAME, cfg.MB_PASSWORD, cfg.MB_SITE)

discogs.user_agent = 'MusicBrainzBot/0.1 +https://github.com/murdos/musicbrainz-bot'

socket.setdefaulttimeout(300)

"""
CREATE TABLE bot_discogs_amz_cover_art (
    gid uuid NOT NULL,
    processed timestamp with time zone DEFAULT now(),
    CONSTRAINT bot_discogs_amz_cover_art_pkey PRIMARY KEY (gid)
);
"""

mbid = sys.argv[1] if len(sys.argv) > 1 else None
if mbid:
    filter_clause = "r.gid = '%s'" % specific_mbid
else:
    filter_clause = "rm.cover_art_presence != 'present'::cover_art_presence"

query = """
WITH
    releases_wo_coverart AS (
        SELECT r.id, discogs_url.url as discogs_url, amz_url.url AS amz_url
        FROM release r
            JOIN release_meta rm ON rm.id = r.id
            JOIN l_release_url discogs_link ON discogs_link.entity0 = r.id AND discogs_link.link IN (SELECT id FROM link WHERE link_type = 76)
            JOIN url discogs_url ON discogs_url.id = discogs_link.entity1
            JOIN l_release_url amz_link ON amz_link.entity0 = r.id AND amz_link.link IN (SELECT id FROM link WHERE link_type = 77)
            JOIN url amz_url ON amz_url.id = amz_link.entity1
            JOIN country rc ON rc.id = r.country AND rc.iso_code = 'FR'
        WHERE """ + filter_clause + """
            /* Discogs link should only be linked to this release */
            AND NOT EXISTS (SELECT 1 FROM l_release_url l WHERE l.entity1 = discogs_url.id AND l.entity0 <> r.id)
            /* this release should not have another Discogs link attached */
            AND NOT EXISTS (SELECT 1 FROM l_release_url l WHERE l.entity0 = r.id AND l.entity1 <> discogs_url.id
                                AND l.link IN (SELECT id FROM link WHERE link_type = 76))
            AND discogs_link.edits_pending = 0
            /* Amazon link should only be linked to this release */
            AND NOT EXISTS (SELECT 1 FROM l_release_url l WHERE l.entity1 = amz_url.id AND l.entity0 <> r.id)
            /* this release should not have another Amazon link attached */
            AND NOT EXISTS (SELECT 1 FROM l_release_url l WHERE l.entity0 = r.id AND l.entity1 <> amz_url.id
                                AND l.link IN (SELECT id FROM link WHERE link_type = 77))
            AND amz_link.edits_pending = 0
            AND r.barcode IS NOT NULL
    )
SELECT r.id, r.gid, r.name, tr.discogs_url, tr.amz_url, ac.name AS artist, r.barcode, b.processed
FROM releases_wo_coverart tr
JOIN s_release r ON tr.id = r.id
JOIN s_artist_credit ac ON r.artist_credit=ac.id
LEFT JOIN bot_discogs_amz_cover_art b ON r.gid = b.gid
ORDER BY b.processed NULLS FIRST, r.artist_credit, r.date_year NULLS LAST, r.name
LIMIT 75
"""

def amz_get_info(url):   
    if url is None:
        return (None, None)
    params = { 'ResponseGroup' : 'Images' }
    
    m = re.match(r'^https?://(?:www.)?amazon\.(.*?)(?:\:[0-9]+)?/.*/([0-9B][0-9A-Z]{9})(?:[^0-9A-Z]|$)', url)
    if m is None:
        return (None, None)
        
    locale = m.group(1).replace('co.', '').replace('com', 'us')
    asin = m.group(2)   
    amazon_api = RetryAPI(cfg.AWS_KEY, cfg.AWS_SECRET_KEY, locale, cfg.AWS_ASSOCIATE_TAG)
    
    try:
        root = amazon_api.item_lookup(asin, **params)
    except amazonproduct.errors.InvalidParameterValue, e:
        return None
    item = root.Items.Item
    if not 'LargeImage' in item.__dict__:
        return (None, None)
    barcode = None
    if 'EAN' in item.__dict__:
        barcode = item.EAN
    elif 'UPC' in item.__dict__:
        barcode = item.UPC
    return (item.LargeImage, barcode)

def discogs_get_primary_image(url):
    if url is None:
        return None
    m = re.match(r'http://www.discogs.com/release/([0-9]+)', url)
    if m:
        release_id = int(m.group(1))
        release = discogs.Release(release_id)
        if 'images' in release.data and len(release.data['images']) >= 1:
            for image in release.data['images']:
                if image['type'] == 'primary':
                    return image
            # No primary image found => return first images
            return release.data['images'][0]
    return None
    
def discogs_get_secondary_images(url):
    if url is None:
        return []
    images = []
    m = re.match(r'http://www.discogs.com/release/([0-9]+)', url)
    if m:
        release_id = int(m.group(1))
        release = discogs.Release(release_id)
        if 'images' in release.data and len(release.data['images']) >= 2:
            for image in release.data['images']:
                if image['type'] == 'secondary':
                    images.append(image)
    return images

for release in db.execute(query):
    colored_out(bcolors.OKBLUE, 'Examining release "%s" by "%s" http://musicbrainz.org/release/%s' % (release['name'], release['artist'], release['gid']))
    colored_out(bcolors.HEADER, ' * Discogs = %s' % (release['discogs_url'],))
    if release['amz_url'] is not None:
        colored_out(bcolors.HEADER, ' * Amazon = %s' % (release['amz_url'],))
    
    # front cover
    discogs_image = discogs_get_primary_image(release['discogs_url'])
    if discogs_image is None:
       discogs_score = 0
       front_uri = None
    else:
        discogs_score = discogs_image['height'] * discogs_image['width']
        front_uri = discogs_image['uri']

    (amz_image, amz_barcode) = amz_get_info(release['amz_url'])
    if amz_barcode is not None and release['barcode'] is not None and re.sub(r'^(0+)', '', amz_barcode) != re.sub(r'^(0+)', '', release['barcode']):
        colored_out(bcolors.FAIL, " * Amz barcode doesn't match MB barcode (%s vs %s) => skipping" % (amz_barcode, release['barcode']))
        continue
        
    if amz_image is not None:
        amz_score = amz_image.Height * amz_image.Width
        colored_out(bcolors.NONE, ' * front cover: AMZ score: %s vs Discogs score: %s' % (amz_score, discogs_score))
        if amz_score > discogs_score:
            front_uri = amz_image.URL.pyval
        
    if front_uri is not None:
        time.sleep(5)
        colored_out(bcolors.OKGREEN, " * Adding front cover art '%s'" % (front_uri,))
        mb.add_cover_art(release['gid'], front_uri, ['front'])

    # other images
    for image in discogs_get_secondary_images(release['discogs_url']):
        colored_out(bcolors.OKGREEN, " * Adding cover art '%s'" % (image['uri'],))
        time.sleep(5)
        mb.add_cover_art(release['gid'], image['uri'])

    out()

    if release['processed'] is None:
        db.execute("INSERT INTO bot_discogs_amz_cover_art (gid) VALUES (%s)", (release['gid'],))
    else:
        db.execute("UPDATE bot_discogs_amz_cover_art SET processed = now() WHERE gid = %s", (release['gid'],))


