-- New York NetTrek area (fa-web-app#916). city_geo holds Census places, and the
-- five boroughs and many suburbs people put as a work city are not places, so
-- those alumni would fall outside every New York list. Reference rows only.
-- ASCII only. ON CONFLICT keeps this safe to re-run.
INSERT INTO city_geo (city_norm, state, lat, lng, county_fips) VALUES
('manhattan','NY',40.7831,-73.9712,'36061'),
('new york city','NY',40.7128,-74.0060,'36061'),
('nyc','NY',40.7128,-74.0060,'36061'),
('harlem','NY',40.8116,-73.9465,'36061'),
('brooklyn','NY',40.6782,-73.9442,'36047'),
('bronx','NY',40.8448,-73.8648,'36005'),
('the bronx','NY',40.8448,-73.8648,'36005'),
('queens','NY',40.7282,-73.7949,'36081'),
('long island city','NY',40.7447,-73.9485,'36081'),
('flushing','NY',40.7654,-73.8318,'36081'),
('astoria','NY',40.7644,-73.9235,'36081'),
('jamaica','NY',40.7027,-73.7890,'36081'),
('staten island','NY',40.5795,-74.1502,'36085'),
('purchase','NY',41.0409,-73.7146,'36119'),
('edison','NJ',40.5187,-74.4121,'34023'),
('bridgewater','NJ',40.5940,-74.6049,'34035'),
('warren','NJ',40.6343,-74.5002,'34035'),
('weehawken','NJ',40.7695,-74.0204,'34017'),
('teaneck','NJ',40.8976,-74.0160,'34003'),
('westport','CT',41.1415,-73.3579,'09001'),
('darien','CT',41.0787,-73.4690,'09001'),
('fairfield','CT',41.1408,-73.2613,'09001')
ON CONFLICT (city_norm, state) DO NOTHING;
