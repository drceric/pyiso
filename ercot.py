import requests
from dateutil.parser import parse as dateutil_parse
import pytz
from datetime import timedelta
from apps.griddata.models import DataPoint
import logging
import copy
import zipfile
import StringIO
from bs4 import BeautifulSoup


class ERCOTClient:
    def __init__(self):
        self.ba_name = 'ERCOT'
        self.base_report_url = 'http://mis.ercot.com'
        
        self.logger = logging.getLogger(__name__)
        
        self.report_type_ids = {
            'wind_5min': '13071',
            'wind_hrly': '13028',
            'gen_hrly': '12358',
        }
        
    def _request_report(self, report_type):
        # request reports list
        params = {'reportTypeId': self.report_type_ids[report_type]}
        report_list_contents = requests.get(self.base_report_url+'/misapp/GetReports.do',
                                            params=params).content
        report_list_soup = BeautifulSoup(report_list_contents)
        
        # find the endpoint to download
        for elt in report_list_soup.find_all('tr'):
            label = elt.find(class_='labelOptional_ind')
            if label:
                if label.string[-3:] == 'csv':
                    report_endpoint = self.base_report_url + elt.a.attrs['href']
                    break
                
        # read report from zip
        r = requests.get(report_endpoint)
        z = zipfile.ZipFile(StringIO.StringIO(r.content)) # have zipfile
        content = z.read(z.namelist()[0]) # have csv
        z.close()
        
        # parse csv
        rows = content.split('\n')
        header = rows[0].split(',')
        raw_data = [dict(zip(header, row.split(','))) for row in rows[1:-1]]
        
        # return
        return raw_data
        
    def _utcify(self, dp, ts_key, dst_key, dst_val):
        naive_local_time = dateutil_parse(dp[ts_key])
        is_dst = dp[dst_key] != dst_val
        aware_local_time = pytz.timezone('US/Central').localize(naive_local_time,
                                                                is_dst=is_dst)
        ts = aware_local_time.astimezone(pytz.utc)
        return ts
        
    def get_generation(self, latest=False, **kwargs):
        # get nonwind gen data
        raw_gen_data = self._request_report('gen_hrly')
        assert len(raw_gen_data) == 1
        total_dp = raw_gen_data[0]
        total_gen = float(total_dp['SE_MW'])
        
        # get timestamp on hour
        raw_ts = self._utcify(total_dp, ts_key='SE_EXE_TIME',
                              dst_key='SE_EXE_TIME_DST', dst_val='s')
        if raw_ts.minute > 30:
            ts_hour_ending = raw_ts.replace(hour=raw_ts.hour+1, minute=0, second=0, microsecond=0)
        else:
            ts_hour_ending = raw_ts.replace(hour=raw_ts.hour, minute=0, second=0, microsecond=0)
        ts_hour_starting = ts_hour_ending - timedelta(hours=1)

        # process wind data
        wind_gen = 0
        for wind_dp in self._request_report('wind_hrly'):
            wind_ts = self._utcify(wind_dp, 'HOUR_ENDING', 'DSTFlag', 'N')
            if wind_ts == ts_hour_ending:
                wind_gen = float(wind_dp['ACTUAL_SYSTEM_WIDE'])
                break
            
        # set up storage
        parsed_data = []
        base_dp = {'timestamp': ts_hour_starting,
                   'freq': DataPoint.HOURLY, 'market': DataPoint.RTHR,
                   'gen_MW': 0, 'ba_name': self.ba_name}

        # collect parsed data
        if wind_gen > 0:
            nonwind_gen = total_gen - wind_gen
            for gen_MW, fuel_name in [(wind_gen, 'wind'), (nonwind_gen, 'nonwind')]:
                parsed_dp = copy.deepcopy(base_dp)
                parsed_dp['fuel_name'] = fuel_name
                parsed_dp['gen_MW'] = gen_MW
                parsed_data.append(parsed_dp)
                
        # return
        return parsed_data