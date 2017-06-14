from ..streams import Stream
from numpy.testing import assert_allclose
import numpy as np


def test_map(exp_db, start_uid1):
    source = Stream()

    def add5(img):
        return img + 5

    L = source.dstarmap(add5,
                        input_info=[('img', 'pe1_image')],
                        output_info=[('img',
                                      {'dtype': 'array',
                                       'source': 'testing'})]).sink_to_list()
    ih1 = exp_db[start_uid1]
    s = exp_db.restream(ih1, fill=True)
    for a in s:
        source.emit(a)
    for l, s in zip(L, exp_db.restream(ih1, fill=True)):
        if l[0] == 'event':
            assert_allclose(l[1]['data']['img'], s[1]['data']['pe1_image'] + 5)


def test_double_map(exp_db, start_uid1):
    source = Stream()
    source2 = Stream()

    def add_imgs(img1, img2):
        return img1 + img2

    L = source.zip(source2).dstarmap(
        add_imgs,
        input_info=[('img1', 'pe1_image'), ('img2', 'pe1_image')],
        output_info=[
            ('img',
             {'dtype': 'array',
              'source': 'testing'})]).sink_to_list()
    ih1 = exp_db[start_uid1]
    s = exp_db.restream(ih1, fill=True)
    for a in s:
        source.emit(a)
        source2.emit(a)
    for l, s in zip(L, exp_db.restream(ih1, fill=True)):
        if l[0] == 'event':
            assert_allclose(l[1]['data']['img'],
                            add_imgs(s[1]['data']['pe1_image'],
                                     s[1]['data']['pe1_image']))
        if l[0] == 'stop':
            assert l[1]['exit_status'] == 'success'


def test_filter(exp_db, start_uid1):
    source = Stream()

    def f(img1):
        return isinstance(img1, np.ndarray)

    L = source.filter(f).sink_to_list()
    ih1 = exp_db[start_uid1]
    s = exp_db.restream(ih1, fill=True)
    for a in s:
        source.emit(a)
    for l, s in zip(L, exp_db.restream(ih1, fill=True)):
        if l[0] == 'event':
            assert_allclose(l[1]['data']['img'], s[1]['data']['pe1_image'])
        if l[0] == 'stop':
            print(l)
            assert l[1]['exit_status'] == 'success'


def test_combine_latest(exp_db, start_uid1, start_uid3):
    source = Stream()
    source2 = Stream()

    L = source.combine_latest(source2).sink_to_list()
    ih1 = exp_db[start_uid1]
    ih2 = exp_db[start_uid3]
    s = exp_db.restream(ih1, fill=True)
    s2 = exp_db.restream(ih2, fill=True)

    def zip_emiter(sources, streams):
        status = [True] * len(sources)
        while all(status):
            for i, (s, stream) in enumerate(zip(sources, streams)):
                if status[i]:
                    try:
                        stream.emit(next(s))
                    except StopIteration:
                        status[i] = False

    zip_emiter((s, s2), (source, source2))
    for l in L:
        print(list(zip(*l))[0])


def test_zip(exp_db, start_uid1, start_uid3):
    source = Stream()
    source2 = Stream()

    L = source.zip(source2).sink_to_list()
    ih1 = exp_db[start_uid1]
    ih2 = exp_db[start_uid3]
    s = exp_db.restream(ih1, fill=True)
    s2 = exp_db.restream(ih2, fill=True)
    for b in s2:
        print(b[0])
        source2.emit(b)
    for a in s:
        print(a[0])
        source.emit(a)
    for l1, l2 in L:
        print(l1, l2)
        assert l1 != l2


def test_workflow(exp_db, start_uid1):
    def subs(x1, x2):
        return x1 - x2
    hdr = exp_db[start_uid1]

    raw_data = hdr.stream(fill=True)
    dark_data = exp_db[hdr['start']['sc_dk_field_uid']].stream(fill=True)
    rds = Stream()
    dark_data_stream = Stream()

    img_stream = rds.zip(dark_data_stream).dstarmap(subs,
                                                    input_info=[
                                                        ('x1', 'pe1_image'),
                                                        ('x2', 'pe1_image')],
                                                    output_info=[('data_key', {
                                                        'dtype': 'array',
                                                        'source': 'testing'})])
    L = img_stream.sink_to_list()

    for d in dark_data:
        dark_data_stream.emit(d)
    for d in raw_data:
        rds.emit(d)
    for l in L:
        print(l)
