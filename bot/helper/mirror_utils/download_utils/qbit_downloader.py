import os
import random
import string
import time
import logging
import shutil
import re
import threading
import qbittorrentapi as qba

from torrentool.api import Torrent
from telegram import InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

from bot import download_dict, download_dict_lock, BASE_URL, dispatcher, get_client, TORRENT_DIRECT_LIMIT, ZIP_UNZIP_LIMIT, STOP_DUPLICATE, WEB_PINCODE, QB_SEED
from bot.helper.mirror_utils.status_utils.qbit_download_status import QbDownloadStatus
from bot.helper.mirror_utils.upload_utils.gdriveTools import GoogleDriveHelper
from bot.helper.telegram_helper.message_utils import sendMessage, sendMarkup, deleteMessage, sendStatusMessage, update_all_messages
from bot.helper.ext_utils.bot_utils import MirrorStatus, getDownloadByGid, get_readable_file_size, get_readable_time
from bot.helper.telegram_helper import button_build

LOGGER = logging.getLogger(__name__)
logging.getLogger('qbittorrentapi').setLevel(logging.ERROR)
logging.getLogger('requests').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.ERROR)



def add_qb_torrent(link, path, listener, select):
    client = get_client()
    pincode = ""
    try:
        if os.path.exists(link):
            is_file = True
            ext_hash = _get_hash_file(link)
        else:
            is_file = False
            ext_hash = _get_hash_magnet(link)
        tor_info = client.torrents_info(torrent_hashes=ext_hash)
        if len(tor_info) > 0:
            sendMessage("This Torrent is already in list.", listener.bot, listener.update)
            client.auth_log_out()
            return
        if is_file:
            op = client.torrents_add(torrent_files=[link], save_path=path)
            os.remove(link)
        else:
            op = client.torrents_add(link, save_path=path)
        time.sleep(0.3)
        if op.lower() == "ok.":
            meta_time = time.time()
            tor_info = client.torrents_info(torrent_hashes=ext_hash)
            if len(tor_info) == 0:
                while True:
                    if time.time() - meta_time >= 30:
                        ermsg = "The Torrent was not added. Report when you see this error"
                        sendMessage(ermsg, listener.bot, listener.update)
                        client.torrents_delete(torrent_hashes=ext_hash, delete_files=True)
                        client.auth_log_out()
                        return
                    tor_info = client.torrents_info(torrent_hashes=ext_hash)
                    if len(tor_info) > 0:
                        break
        else:
            sendMessage("This is an unsupported/invalid link.", listener.bot, listener.update)
            client.torrents_delete(torrent_hashes=ext_hash, delete_files=True)
            client.auth_log_out()
            return
        tor_info = tor_info[0]
        ext_hash = tor_info.hash
        gid = ''.join(random.SystemRandom().choices(string.ascii_letters + string.digits, k=14))
        with download_dict_lock:
            download_dict[listener.uid] = QbDownloadStatus(listener, client, gid, ext_hash, select)
        LOGGER.info(f"QbitDownload started: {tor_info.name} - Hash: {ext_hash}")
        threading.Thread(target=_qb_listener, args=(listener, client, gid, ext_hash, select, meta_time, path)).start()
        if BASE_URL is not None and select:
            if not is_file:
                metamsg = "Downloading Metadata, wait then you can select files or mirror torrent file"
                meta = sendMessage(metamsg, listener.bot, listener.update)
                while True:
                    tor_info = client.torrents_info(torrent_hashes=ext_hash)
                    if len(tor_info) == 0:
                        deleteMessage(listener.bot, meta)
                        return
                    try:
                        tor_info = tor_info[0]
                        if tor_info.state in ["metaDL", "checkingResumeData"]:
                            time.sleep(1)
                        else:
                            deleteMessage(listener.bot, meta)
                            break
                    except:
                        deleteMessage(listener.bot, meta)
                        return
            time.sleep(0.5)
            client.torrents_pause(torrent_hashes=ext_hash)
            for n in str(ext_hash):
                if n.isdigit():
                    pincode += str(n)
                if len(pincode) == 4:
                    break
            buttons = button_build.ButtonMaker()
            if WEB_PINCODE:
                buttons.buildbutton("Select Files", f"{BASE_URL}/app/files/{ext_hash}")
                buttons.sbutton("Pincode", f"pin {gid} {pincode}")
            else:
                buttons.buildbutton("Select Files", f"{BASE_URL}/app/files/{ext_hash}?pin_code={pincode}")
            buttons.sbutton("Done Selecting", f"done {gid} {ext_hash}")
            QBBUTTONS = InlineKeyboardMarkup(buttons.build_menu(2))
            msg = "Your download paused. Choose files then press Done Selecting button to start downloading."
            sendMarkup(msg, listener.bot, listener.update, QBBUTTONS)
        else:
            sendStatusMessage(listener.update, listener.bot)
    except qba.UnsupportedMediaType415Error as e:
        LOGGER.error(str(e))
        sendMessage(f"This is an unsupported/invalid link: {str(e)}", listener.bot, listener.update)
        client.auth_log_out()
    except Exception as e:
        sendMessage(str(e), listener.bot, listener.update)
        client.auth_log_out()

def _qb_listener(listener, client, gid, ext_hash, select, meta_time, path):
    stalled_time = time.time()
    uploaded = False
    sizeChecked = False
    dupChecked = False
    rechecked = False
    get_info = 0
    while True:
        time.sleep(4)
        tor_info = client.torrents_info(torrent_hashes=ext_hash)
        if len(tor_info) == 0:
            get_info += 1
            if get_info > 2:
                client.auth_log_out()
                break
            continue
        get_info = 0
        try:
            tor_info = tor_info[0]
            if tor_info.state == "metaDL":
                stalled_time = time.time()
                if time.time() - meta_time >= 999: # timeout while downloading metadata
                    client.torrents_pause(torrent_hashes=ext_hash)
                    time.sleep(0.3)
                    listener.onDownloadError("Dead Torrent!")
                    client.torrents_delete(torrent_hashes=ext_hash)
                    client.auth_log_out()
                    break
            elif tor_info.state == "downloading":
                stalled_time = time.time()
                if STOP_DUPLICATE and not listener.isLeech and not dupChecked and os.path.isdir(f'{path}'):
                    LOGGER.info('Checking File/Folder if already in Drive')
                    qbname = str(os.listdir(f'{path}')[-1])
                    if qbname.endswith('.!qB'):
                        qbname = os.path.splitext(qbname)[0]
                    if listener.isZip:
                        qbname = qbname + ".zip"
                    if not listener.extract:
                        gd = GoogleDriveHelper()
                        qbmsg, button = gd.drive_list(qbname, True)
                        if qbmsg:
                            msg = "File/Folder is already available in Drive."
                            client.torrents_pause(torrent_hashes=ext_hash)
                            time.sleep(0.3)
                            listener.onDownloadError(msg)
                            sendMarkup("Here are the search results:", listener.bot, listener.update, button)
                            client.torrents_delete(torrent_hashes=ext_hash)
                            client.auth_log_out()
                            break
                    dupChecked = True
                if not sizeChecked:
                    limit = None
                    if ZIP_UNZIP_LIMIT is not None and (listener.isZip or listener.extract):
                        mssg = f'Zip/Unzip limit is {ZIP_UNZIP_LIMIT}GB'
                        limit = ZIP_UNZIP_LIMIT
                    elif TORRENT_DIRECT_LIMIT is not None:
                        mssg = f'Torrent limit is {TORRENT_DIRECT_LIMIT}GB'
                        limit = TORRENT_DIRECT_LIMIT
                    if limit is not None:
                        LOGGER.info('Checking File/Folder Size...')
                        time.sleep(1)
                        size = tor_info.size
                        if size > limit * 1024**3:
                            client.torrents_pause(torrent_hashes=ext_hash)
                            time.sleep(0.3)
                            listener.onDownloadError(f"{mssg}.\nYour File/Folder size is {get_readable_file_size(size)}")
                            client.torrents_delete(torrent_hashes=ext_hash)
                            client.auth_log_out()
                            break
                    sizeChecked = True
            elif tor_info.state == "stalledDL":
                if not rechecked and 0.99989999999999999 < tor_info.progress < 1:
                    LOGGER.info(f"Force recheck - Name: {tor_info.name} Hash: {ext_hash} Downloaded Bytes: {tor_info.downloaded} Size: {tor_info.size} Total Size: {tor_info.total_size}")
                    client.torrents_recheck(torrent_hashes=ext_hash)
                    rechecked = True
                elif time.time() - stalled_time >= 9999: # timeout after downloading metadata
                    client.torrents_pause(torrent_hashes=ext_hash)
                    time.sleep(0.3)
                    listener.onDownloadError("Dead Torrent!")
                    client.torrents_delete(torrent_hashes=ext_hash)
                    client.auth_log_out()
                    break
            elif tor_info.state == "missingFiles":
                client.torrents_recheck(torrent_hashes=ext_hash)
            elif tor_info.state == "error":
                client.torrents_pause(torrent_hashes=ext_hash)
                time.sleep(0.3)
                listener.onDownloadError("No enough space for this torrent on device")
                client.torrents_delete(torrent_hashes=ext_hash)
                client.auth_log_out()
                break
            elif tor_info.state in ["uploading", "queuedUP", "stalledUP", "forcedUP"] and not uploaded:
                uploaded = True
                if not QB_SEED:
                    client.torrents_pause(torrent_hashes=ext_hash)
                if select:
                    for dirpath, subdir, files in os.walk(f"{path}", topdown=False):
                        for filee in files:
                            if filee.endswith(".!qB") or filee.endswith('.parts') and filee.startswith('.'):
                                os.remove(os.path.join(dirpath, filee))
                        for folder in subdir:
                            if folder == ".unwanted":
                                shutil.rmtree(os.path.join(dirpath, folder))
                    for dirpath, subdir, files in os.walk(f"{path}", topdown=False):
                        if not os.listdir(dirpath):
                            os.rmdir(dirpath)
                listener.onDownloadComplete()
                if QB_SEED:
                    with download_dict_lock:
                        download_dict[listener.uid] = QbDownloadStatus(listener, client, gid, ext_hash, select)
                    update_all_messages()
                    LOGGER.info(f"Seeding started: {tor_info.name}")
                else:
                    client.torrents_delete(torrent_hashes=ext_hash)
                    client.auth_log_out()
                    break
            elif tor_info.state == 'pausedUP' and QB_SEED:
                listener.onUploadError(f"Seeding stopped with Ratio: {round(tor_info.ratio, 3)} and Time: {get_readable_time(tor_info.seeding_time)}")
                client.torrents_delete(torrent_hashes=ext_hash)
                client.auth_log_out()
                break
        except:
            pass

def get_confirm(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    data = data.split(" ")
    qbdl = getDownloadByGid(data[1])
    if qbdl is None:
        query.answer(text="This task has been cancelled!", show_alert=True)
        query.message.delete()
    elif user_id != qbdl.listener().message.from_user.id:
        query.answer(text="Don't waste your time!", show_alert=True)
    elif data[0] == "pin":
        query.answer(text=data[2], show_alert=True)
    elif data[0] == "done":
        query.answer()
        qbdl.client().torrents_resume(torrent_hashes=data[2])
        sendStatusMessage(qbdl.listener().update, qbdl.listener().bot)
        query.message.delete()

def _get_hash_magnet(mgt):
    if mgt.startswith('magnet:'):
        mHash = re.search(r'(?<=xt=urn:btih:)[a-zA-Z0-9]+', mgt).group(0)
        return mHash.lower()

def _get_hash_file(path):
    tr = Torrent.from_file(path)
    mgt = tr.magnet_link
    return _get_hash_magnet(mgt)


pin_handler = CallbackQueryHandler(get_confirm, pattern="pin", run_async=True)
done_handler = CallbackQueryHandler(get_confirm, pattern="done", run_async=True)
dispatcher.add_handler(pin_handler)
dispatcher.add_handler(done_handler)
