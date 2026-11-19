from financas.secrets import _unhex


def test_multiline_hex_is_decoded():
    pem = "-----BEGIN X-----\nabc\n-----END X-----\n"
    assert _unhex(pem.encode().hex()) == pem


def test_plain_values_untouched():
    assert _unhex("95980946955783376001444") == "95980946955783376001444"
    assert _unhex("1234abcd") == "1234abcd"
    assert _unhex("zpub6abc") == "zpub6abc"
