"""Windows current-user DPAPI storage; no plaintext secret file or shared key."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path


def location():
    return Path(os.environ.get('LOCALAPPDATA',Path.home()))/'HubbleScanner'/'telegram.dpapi'


def ads_token_location():
    return Path(os.environ.get('LOCALAPPDATA',Path.home()))/'HubbleScanner'/'ads-token.dpapi'


def transform(data,decrypt=False):
    if os.name!='nt':raise RuntimeError('Encrypted notifications require Windows DPAPI')
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)));dest=Blob()
    dll=ctypes.WinDLL('crypt32',use_last_error=True)
    fn=dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    fn.restype=wintypes.BOOL
    if not fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(dest)):
        raise ctypes.WinError(ctypes.get_last_error())
    free=ctypes.WinDLL('kernel32').LocalFree;free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
    try:return ctypes.string_at(dest.data,dest.size)
    finally:free(dest.data)


def save(token,chat,path=None):
    if not token.strip() or not chat.strip():raise ValueError('Token and chat are required')
    p=Path(path) if path else location();p.parent.mkdir(parents=True,exist_ok=True)
    blob=transform(json.dumps({'token':token.strip(),'chat':chat.strip()}).encode())
    tmp=p.with_suffix('.tmp');tmp.write_bytes(blob);os.replace(tmp,p)


def load(path=None):
    p=Path(path) if path else location()
    if not p.exists():return None
    return json.loads(transform(p.read_bytes(),decrypt=True))


def save_ads_token(token,path=None):
    """Store the ADS API token encrypted for this Windows user only."""
    if not token or not token.strip():raise ValueError('ADS token is required')
    p=Path(path) if path else ads_token_location();p.parent.mkdir(parents=True,exist_ok=True)
    blob=transform(token.strip().encode('utf-8'))
    tmp=p.with_suffix('.tmp');tmp.write_bytes(blob);os.replace(tmp,p)


def load_ads_token(path=None):
    p=Path(path) if path else ads_token_location()
    if not p.exists():return None
    return transform(p.read_bytes(),decrypt=True).decode('utf-8')


def delete_ads_token(path=None):
    p=Path(path) if path else ads_token_location()
    p.unlink(missing_ok=True)


if __name__=='__main__':
    import getpass
    save(getpass.getpass('Telegram bot token (hidden): '),input('Destination chat ID: '))
    print('Saved with Windows current-user encryption. No token is shared with workers.')
