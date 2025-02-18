'''Extract fonts properties from PDF.

Font properties like font name, size are covered in :py:class:`~pdf2docxnogui.text.TextSpan`, 
but more generic properties are required further:

* Font family name. The font name extracted and set in `TextSpan` might not valid when 
  directly used in MS Word, e.g. "ArialMT" should be "Arial". So, we need to get font
  family name, which should be accepted by MS Word, based on the font file itself.

* Font line height ratio. As line height = font_size * line_height_ratio, it's used to 
  calculate relative line spacing. In general, 1.12 is an approximate value to this ratio,
  but it's in fact a font-related value, especially for CJK font.
'''

import os
import logging
from io import BytesIO
from collections import namedtuple 
from fontTools.ttLib import TTFont, TTLibError
from ..common.Collection import BaseCollection
from ..common.constants import (CJK_CODEPAGE_BITS, CJK_UNICODE_RANGE_BITS, 
                                    CJK_UNICODE_RANGES, DICT_FONT_LINE_HEIGHT)


Font = namedtuple('Font', [ 'descriptor',     # font descriptor
                            'name',           # real font name
                            'line_height'])   # standard line height ratio


class Fonts(BaseCollection):
    '''Extracted fonts properties from PDF.'''

    def get(self, font_name:str, default:Font=None):
        '''Get matched font by font name, or return default font.'''
        target = self._to_descriptor(font_name)

        # 1st priority: check right the name
        for font in self:
            if target==font.descriptor: return font
        
        # 2nd priority: target name is contained in font name
        for font in self:
            if target in font.descriptor: return font

        # 3rd priority: target name contains font name
        for font in self:
            if font.descriptor in target: return font
        
        # show warning msg if not found
        if default:
            logging.warning('Replace font "%s" with "%s" due to lack of data.', font_name, default.name)
        return Font(descriptor=target, name=default.name, line_height=default.line_height)


    @classmethod
    def extract(cls, fitz_doc, default_font:Font):
        '''Extract fonts from PDF and get properties.
        * Only embedded fonts (v.s. the base 14 fonts) can be extracted.
        * The extracted fonts may be invalid due to reason from PDF file itself.
        * Check a default font table for those failed cases.
        '''        
        # get unique font references
        xrefs = set()
        for page in fitz_doc:
            for f in page.get_fonts(): xrefs.add(f[0])

        # process xref one by one
        default_fonts = cls.get_defult_fonts()
        fonts = []
        for xref in xrefs:
            valid = False
            basename, ext, _, buffer = fitz_doc.extract_font(xref)
            name = cls._normalized_font_name(basename)
            if ext not in ('n/a', 'ccf'): # embedded fonts, or not supported fonts
                try:
                    tt = TTFont(BytesIO(buffer))
                except TTLibError:
                    tt = None

                # valid true type font, no matter installed in the system or not
                if cls._is_valid(tt):
                    name = cls.get_font_family_name(tt)
                    fonts.append(Font(
                        descriptor=cls._to_descriptor(name),
                        name=name,
                        line_height=cls.get_line_height_factor(tt)))
                    valid = True
                
            # check default font if not valid
            if not valid:
                font = default_fonts.get(name, default_font)
                if font: fonts.append(font)
        
        return cls(fonts)



    @classmethod
    def get_defult_fonts(cls):
        '''Default fonts, e.g. base 14 font and pre-defined fonts.'''
        fonts = [Font(descriptor=cls._to_descriptor(name), 
                        name=name, 
                        line_height=f) for name, f in DICT_FONT_LINE_HEIGHT.items()]
        return cls(fonts)

    
    @classmethod
    def get_defult_font(cls, default_name:str):
        '''Get default font by name.'''
        font = cls.get_defult_fonts().get(default_name, None)
        if not font:
            font = Font(descriptor=cls._to_descriptor(default_name),
                        name=default_name, 
                        line_height=1.20) # an approximate value
        return font

    
    @staticmethod
    def _is_valid(tt_font:TTFont):
        if not tt_font: return False
        for key in ('name', 'hhea', 'head', 'OS/2', 'cmap'):
            if not tt_font.has_key(key): return False
        return True


    @staticmethod
    def _normalized_font_name(name):
        '''Normalize raw font name, e.g. BCDGEE+Calibri-Bold, BCDGEE+Calibri -> Calibri.'''
        return name.split('+')[-1].split('-')[0]


    @staticmethod
    def _to_descriptor(name:str):
        '''Remove potential space, dash in font name, and turn to upper case.'''
        return name.replace(' ', '').replace('-', '').upper()

    
    @staticmethod
    def get_font_family_name(tt_font:TTFont):
        '''Get the font family name from the font's names table.

        https://gist.github.com/pklaus/dce37521579513c574d0
        '''
        name = family = ''
        FONT_SPECIFIER_NAME_ID = 4
        FONT_SPECIFIER_FAMILY_ID = 1

        for record in tt_font['name'].names:
            if b'\x00' in record.string:
                name_str = record.string.decode('utf-16-be')
            else:   
                name_str = record.string.decode('latin-1')

            if record.nameID == FONT_SPECIFIER_NAME_ID and not name:
                name = name_str
            elif record.nameID == FONT_SPECIFIER_FAMILY_ID and not family: 
                family = name_str

            if name and family: break

        # in case the font name is modified to pattern like BCDGEE+Calibri-Bold
        return Fonts._normalized_font_name(family)


    @staticmethod
    def get_line_height_factor(tt_font:TTFont):
        '''Calculate line height ratio based on ``hhea`` and ``OS/2`` tables.

        Fon non-CJK fonts::

            f = (hhea.Ascent - hhea.Descent + hhea.LineGap) / units_per_em
        
        For non-CJK fonts (Windows)::

            f = (OS/2.winAscent + OS/2.winDescent + [External Leading]) / units_per_em
            External Leading = MAX(0, hhea.LineGap - ((OS/2.WinAscent + OS/2.winDescent) - (hhea.Ascent - hhea.Descent)))

        For CJK fonts::

            f = 1.3 * (hhea.Ascent - hhea.Descent) / units_per_em

        Read more:
        * https://docs.microsoft.com/en-us/typography/opentype/spec/recom#baseline-to-baseline-distances
        * https://github.com/source-foundry/font-line#baseline-to-baseline-distance-calculations
        * https://www.zhihu.com/question/23349103
        * https://github.com/source-foundry/font-line/blob/master/lib/fontline/metrics.py
        '''
        units_per_em = tt_font["head"].unitsPerEm

        # hhea metrics
        hhea = tt_font["hhea"]
        hhea_ascent = hhea.ascent
        hhea_descent = hhea.descent
        hhea_linegap = hhea.lineGap

        hhea_total_height = hhea_ascent + abs(hhea_descent)
        hhea_btb_distance =  hhea_total_height + hhea_linegap

        # depends on System
        if os.name=='nt':

            # OS/2 metrics
            os2 = tt_font["OS/2"]
            os2_win_ascent = os2.usWinAscent
            os2_win_descent = os2.usWinDescent
            os2_win_total_height = os2_win_ascent + os2_win_descent
            win_external_leading = max(0.0, hhea_linegap-(os2_win_total_height-hhea_total_height))
            win_btb_distance = os2_win_total_height + win_external_leading
            
            btb_distance = win_btb_distance
        
        else:
            btb_distance = hhea_btb_distance

        # depends on CJK font or not
        cjk = Fonts.is_cjk_font(tt_font)
        distance = 1.3*hhea_total_height if cjk else 1.0*btb_distance

        return distance / units_per_em
    

    @staticmethod
    def is_cjk_font(tt_font:TTFont):
        '''Test font object to confirm that it meets our definition of a CJK font file.

        The definition is met if any of the following conditions are True:
        1. The font has a CJK code page bit set in the OS/2 table
        2. The font has a CJK Unicode range bit set in the OS/2 table
        3. The font has any CJK Unicode code points defined in the cmap table

        https://github.com/googlefonts/fontbakery/blob/main/Lib/fontbakery/profiles/shared_conditions.py
        '''
        os2 = tt_font["OS/2"]

        # OS/2 code page checks
        for _, bit in CJK_CODEPAGE_BITS.items():
            if hasattr(os2, 'ulCodePageRange1') and os2.ulCodePageRange1 & (1 << bit):
                return True

        # OS/2 Unicode range checks
        for _, bit in CJK_UNICODE_RANGE_BITS.items():
            if bit in range(0, 32):
                if hasattr(os2, 'ulCodePageRange1') and os2.ulUnicodeRange1 & (1 << bit):
                    return True

            elif bit in range(32, 64):
                if hasattr(os2, 'ulCodePageRange2') and os2.ulUnicodeRange2 & (1 << (bit-32)):
                    return True

            elif bit in range(64, 96):
                if hasattr(os2, 'ulCodePageRange3') and os2.ulUnicodeRange3 & (1 << (bit-64)):
                    return True

        # defined CJK Unicode code point in cmap table checks
        try:
            cmap = tt_font.getBestCmap()
        except AssertionError:
            return False
        if not cmap: return False

        for unicode_range in CJK_UNICODE_RANGES:
            for x in range(unicode_range[0], unicode_range[1]+1):
                if int(x) in cmap:
                    return True

        # default, return False if the above checks did not identify a CJK font
        return False
