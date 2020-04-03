from pprint import pprint, pformat
from struct import unpack
from sys import platform
import asyncio
import glob
import json
import logging
import math
import os
import pkg_resources
import re
import shutil
import signal
import time
import traceback
import uuid
import subprocess
import pickle
from google.protobuf.message import DecodeError
from google.protobuf.json_format import MessageToDict
from grpclib.server import Server

from dotaservice import __version__
from dotaservice.protos.dota_gcmessages_common_bot_script_pb2 import CMsgBotWorldState
from dotaservice.protos.dota_shared_enums_pb2 import DOTA_GAMERULES_STATE_GAME_IN_PROGRESS
from dotaservice.protos.dota_shared_enums_pb2 import DOTA_GAMERULES_STATE_PRE_GAME
from dotaservice.protos.DotaService_grpc import DotaServiceBase
from dotaservice.protos.DotaService_pb2 import Empty
from dotaservice.protos.DotaService_pb2 import HOST_MODE_DEDICATED, HOST_MODE_GUI, HOST_MODE_GUI_MENU
from dotaservice.protos.DotaService_pb2 import TEAM_RADIANT, TEAM_DIRE
from dotaservice.protos.DotaService_pb2 import Observation, ObserveConfig, InitialObservation
from dotaservice.protos.DotaService_pb2 import Status
from dotaservice.protos.DotaService_pb2 import Player, Team, Hero

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s')
logger = logging.getLogger(__name__)

LUA_FILES_GLOB = pkg_resources.resource_filename('dotaservice', 'lua/*.lua')
LUA_FILES_GLOB_ACTIONS = pkg_resources.resource_filename('dotaservice', 'lua/actions/*.lua')


def verify_game_path(game_path):
    if not os.path.exists(game_path):
        raise ValueError("Game path '{}' does not exist.".format(game_path))
    if not os.path.isdir(game_path):
        raise ValueError("Game path '{}' is not a directory.".format(game_path))
    dota_script = os.path.join(game_path, DotaGame.DOTA_SCRIPT_FILENAME)
    if not os.path.isfile(dota_script):
        raise ValueError("Dota executable '{}' is not a file.".format(dota_script))
    if not os.access(dota_script, os.X_OK):
        raise ValueError("Dota executable '{}' is not executable.".format(dota_script))


class DotaGame(object):

    ACTIONS_FILENAME_FMT = 'actions_t{team_id}'
    ACTIONABLE_GAME_STATES = [DOTA_GAMERULES_STATE_PRE_GAME, DOTA_GAMERULES_STATE_GAME_IN_PROGRESS]
    BOTS_FOLDER_NAME = 'bots'
    CONFIG_FILENAME = 'config_auto'
    CONSOLE_LOG_FILENAME = 'console.log'
    CONSOLE_LOGS_GLOB = 'console*.log'
    DOTA_SCRIPT_FILENAME = 'dota.sh'
    LIVE_CONFIG_FILENAME = 'live_config_auto'
    PORT_WORLDSTATES = {TEAM_RADIANT: 12120, TEAM_DIRE: 12121} 
    RE_DEMO =  re.compile(r'playdemo[ \t](.*dem)')
    RE_LUARDY = re.compile(r'LUARDY[ \t](\{.*\})')
    RE_PLAYERS = re.compile(r'PLYRS[ \t](\[.*\])')
    RE_WIN = re.compile(r'good guys win = (\d)')
    WORLDSTATE_PAYLOAD_BYTES = 4

    def __init__(self,
                 dota_path,
                 action_folder,
                 remove_logs,
                 host_timescale,
                 ticks_per_observation,
                 hero_picks,
                 game_mode,
                 host_mode,
                 game_id=None,
                 ):
        self.dota_path = dota_path
        self.action_folder = action_folder
        self.remove_logs = remove_logs
        self.host_timescale = host_timescale
        self.ticks_per_observation = ticks_per_observation
        self.hero_picks = hero_picks
        self.game_mode = game_mode
        self.host_mode = host_mode
        self.game_id = game_id
        if not self.game_id:
            self.game_id = str(uuid.uuid1())
        logger.info('Creating game_id={}'.format(self.game_id))
        self.dota_bot_path = os.path.join(self.dota_path, 'dota', 'scripts', 'vscripts',
                                          self.BOTS_FOLDER_NAME)
        self.bot_path = self._create_bot_path()
        self.worldstate_queues = {
            TEAM_RADIANT: asyncio.Queue(loop=asyncio.get_event_loop()),
            TEAM_DIRE: asyncio.Queue(loop=asyncio.get_event_loop()),
        }
        # self.lua_config_future = asyncio.get_event_loop().create_future()
        self._write_config()
        self.process = None
        self.players = None
        self.demo_path_rel = None

        if self.host_mode != HOST_MODE_DEDICATED:
            # TODO(tzaman): Change the proto so that there are per-hostmode settings?
            self.host_timescale = 1

        if len(hero_picks) != 10:
            raise ValueError('Expected 10 hero picks. Got {}.'.format(len(hero_picks)))

        has_display = 'DISPLAY' in os.environ or platform == "darwin"
        if not has_display and host_mode != HOST_MODE_DEDICATED:
            raise ValueError('GUI requested but no display detected.')
        super().__init__()

    def _write_config(self):
        # Write out the game configuration.
        config = {
            'game_id': self.game_id,
            'ticks_per_observation': self.ticks_per_observation,
            'hero_picks': [MessageToDict(h) for h in self.hero_picks],
        }
        self.write_static_config(data=config)

    def write_static_config(self, data):
        self._write_bot_data_file(filename_stem=self.CONFIG_FILENAME, data=data)

    def write_live_config(self, data):
        logger.debug('Writing live_config={}'.format(data))
        self._write_bot_data_file(filename_stem=self.LIVE_CONFIG_FILENAME, data=data)
        
    def write_action(self, data, team_id):
        filename_stem = self.ACTIONS_FILENAME_FMT.format(team_id=team_id)
        self._write_bot_data_file(filename_stem=filename_stem, data=data)

    def _write_bot_data_file(self, filename_stem, data):
        """Write a file to lua to that the bot can read it.

        Although writing atomicly would prevent bad reads, we just catch the bad reads in the
        dota bot client.
        """
        filename = os.path.join(self.bot_path, '{}.lua'.format(filename_stem))
        data = """return '{data}'""".format(data=json.dumps(data, separators=(',', ':')))
        with open(filename, 'w') as f:
            f.write(data)

    def _create_bot_path(self):
        """Remove DOTA's bots subdirectory or symlink and update it with our own."""
        if os.path.exists(self.dota_bot_path) or os.path.islink(self.dota_bot_path):
            if os.path.isdir(self.dota_bot_path) and not os.path.islink(self.dota_bot_path):
                raise ValueError(
                    'There is already a bots directory ({})! Please remove manually.'.format(
                        self.dota_bot_path))
            os.remove(self.dota_bot_path)
        session_folder = os.path.join(self.action_folder, str(self.game_id))
        os.mkdir(session_folder)
        bot_path = os.path.join(session_folder, self.BOTS_FOLDER_NAME)
        os.mkdir(bot_path)

        # Copy all the bot files into the action folder.
        lua_files = glob.glob(LUA_FILES_GLOB)
        for filename in lua_files:
            shutil.copy(filename, bot_path)

        # Copy all the bot action files into the actions subdirectory
        action_path = os.path.join(bot_path, "actions")
        os.mkdir(action_path)
        action_files = glob.glob(LUA_FILES_GLOB_ACTIONS)
        for filename in action_files:
            shutil.copy(filename, action_path)

        # shutil.copy('/Users/tzaman/dev/dotaservice/botcpp/botcpp_radiant.so', bot_path)

        # Finally, symlink DOTA to this folder.
        os.symlink(src=bot_path, dst=self.dota_bot_path)
        return bot_path

    def run(self):
        self._run_dota()

    def _run_dota(self):
        script_path = os.path.join(self.dota_path, self.DOTA_SCRIPT_FILENAME)

        # TODO(tzaman): all these options should be put in a proto and parsed with gRPC Config.
        args = [
            script_path,
            '-botworldstatesocket_threaded',
            '-botworldstatetosocket_frames', '{}'.format(self.ticks_per_observation),
            '-botworldstatetosocket_radiant', '{}'.format(self.PORT_WORLDSTATES[TEAM_RADIANT]),
            '-botworldstatetosocket_dire', '{}'.format(self.PORT_WORLDSTATES[TEAM_DIRE]),
            '-con_logfile', 'scripts/vscripts/bots/{}'.format(self.CONSOLE_LOG_FILENAME),
            '-con_timestamp',
            '-console',
            '-dev',
            '-insecure',
            '-noip',
            '-nowatchdog',  # WatchDog will quit the game if e.g. the lua api takes a few seconds.
            '+clientport', '27006',  # Relates to steam client.
            '+dota_1v1_skip_strategy', '1',
            '+dota_bot_set_difficulty', '3',
            '+dota_surrender_on_disconnect', '0',
            '+host_timescale', '{}'.format(self.host_timescale),
            '+hostname dotaservice',
            '+sv_cheats', '1',
            '+sv_hibernate_when_empty', '0',
            '+tv_delay', '0',
            '+tv_enable', '1',
            '+tv_title', '{}'.format(self.game_id),
            '+tv_autorecord', '1',
            '+tv_transmitall', '1',  # TODO(tzaman): what does this do exactly?
        ]

        if self.host_mode == HOST_MODE_DEDICATED:
            args.append('-dedicated')
        if self.host_mode == HOST_MODE_DEDICATED or \
            self.host_mode == HOST_MODE_GUI:
            args.append('-fill_with_bots')
            args.extend(['+map', 'start', 'gamemode', '{}'.format(self.game_mode)])
            args.extend(['+sv_lan', '1'])
        if self.host_mode == HOST_MODE_GUI_MENU:
            args.extend(['+sv_lan', '0'])

        # Supress stdout if the logger level is info.
        stdout = None if logger.level == 'INFO' else asyncio.subprocess.PIPE
        print(*args)
        subprocess.call(args)

    def _move_recording(self):
        logger.info('::_move_recording')
        # Move the recording.
        # TODO(tzaman): high-level: make recordings optional.
        # TODO(tzaman): retain discarded recordings:
        #   EXAMPLE FROM CONSOLE:
        #   EX: "Discarding replay replays/auto-20181228-2311-start-dotaservice.dem"
        #   EX: "Renamed replay replays/auto-20181228-2311-start-dotaservice.dem to replays/discarded/replays/auto-20181228-2311-start-dotaservice.dem"
        if self.demo_path_rel is not None:
            demo_path_abs = os.path.join(self.dota_path, 'dota', self.demo_path_rel)
            try:
                shutil.move(demo_path_abs, self.bot_path)
            except Exception as e:  # Fail silently.
                logger.error(e)

class DotaService():

    END_STATES = {
        None: Status.Value('RESOURCE_EXHAUSTED'),
        TEAM_RADIANT: Status.Value('RADIANT_WIN'),
        TEAM_DIRE: Status.Value('DIRE_WIN'),
    }

    def __init__(self, dota_path, action_folder, remove_logs):
        self.dota_path = dota_path
        self.action_folder = action_folder
        self.remove_logs = remove_logs

        # Initial assertions.
        verify_game_path(self.dota_path)

        if not os.path.exists(self.action_folder):
            if platform == "linux" or platform == "linux2":
                raise ValueError(
                    "Action folder '{}' not found.\nYou can create a 2GB ramdisk by executing:"
                    "`mkdir /tmpfs; mount -t tmpfs -o size=2048M tmpfs /tmpfs`\n"
                    "With Docker, you can add a tmpfs adding `--mount type=tmpfs,destination=/tmpfs`"
                    " to its run command.".format(self.action_folder))
            elif platform == "darwin":
                if not os.path.exists(self.action_folder):
                    raise ValueError(
                        "Action folder '{}' not found.\nYou can create a 2GB ramdisk by executing:"
                        " `diskutil erasevolume HFS+ 'ramdisk' `hdiutil attach -nomount ram://4194304``"
                        .format(self.action_folder))

        self.dota_game = None
        super().__init__()

    @property
    def observe_timeout(self):
        if self.dota_game.host_mode == HOST_MODE_DEDICATED:
            return 10
        return 3600

    @staticmethod
    def stop_dota_pids():
        """Stop all dota processes.
        
        Stopping dota is nessecary because only one client can be active at a time. So we clean
        up anything that already existed earlier, or a (hanging) mess we might have created.
        """
        dota_pids = str.split(os.popen("ps -e | grep dota2 | awk '{print $1}'").read())
        for pid in dota_pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def clean_resources(self):
        """Clean resoruces.
        
        Kill any previously running dota processes, and therefore set our status to ready.
        """
        # TODO(tzaman): Currently semi-gracefully. Can be cleaner.
        if self.dota_game is not None:
            # await self.dota_game.close()
            self.dota_game = None
        self.stop_dota_pids()

    def reset(self, config):
        """reset method.

        This method should start up the dota game and the other required services.
        """
        print('DotaService::reset()')

        logger.debug('config=\n{}'.format(config))

        self.clean_resources()

        # Create a new dota game instance.
        self.dota_game = DotaGame(
            dota_path=self.dota_path,
            action_folder=self.action_folder,
            remove_logs=self.remove_logs,
            host_timescale=config.host_timescale,
            ticks_per_observation=config.ticks_per_observation,
            hero_picks=config.hero_picks,
            game_mode=config.game_mode,
            host_mode=config.host_mode,
            game_id=config.game_id,
        )
        print(f'timescale: {self.dota_game.host_timescale}')

        # Start dota.
        self.dota_game.run()

    @staticmethod
    def players_to_pb(players):
        players_pb = []
        for player in players:
            players_pb.append(
                Player(
                    id=player['id'],
                    team_id=player['team_id'],
                    is_bot=player['is_bot'],
                    hero=player['hero'].upper(),
                )
            )
        return players_pb


def main(grpc_host, grpc_port, dota_path, action_folder, remove_logs, log_level):
    logger.setLevel(log_level)
    dota_service = DotaService(
        dota_path=dota_path,
        action_folder=action_folder,
        remove_logs=remove_logs,
        )
    with open('/tmp/config.pickle', 'rb') as f:
        config = pickle.load(f)
    dota_service.reset(config)
