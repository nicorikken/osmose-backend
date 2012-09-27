#-*- coding: utf-8 -*-

###########################################################################
##                                                                       ##
## Copyrights Etienne Chové <chove@crans.org> 2009                       ##
##                                                                       ##
## This program is free software: you can redistribute it and/or modify  ##
## it under the terms of the GNU General Public License as published by  ##
## the Free Software Foundation, either version 3 of the License, or     ##
## (at your option) any later version.                                   ##
##                                                                       ##
## This program is distributed in the hope that it will be useful,       ##
## but WITHOUT ANY WARRANTY; without even the implied warranty of        ##
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         ##
## GNU General Public License for more details.                          ##
##                                                                       ##
## You should have received a copy of the GNU General Public License     ##
## along with this program.  If not, see <http://www.gnu.org/licenses/>. ##
##                                                                       ##
###########################################################################

from plugins.Plugin import Plugin
import re


class TagRemove_NameIsRef(Plugin):

    only_for = ["FR"]

    def init(self, logger):
        Plugin.init(self, logger)
        self.errors[904] = { "item": 4040, "level": 1, "tag": ["name", "highway", "ref"], "desc": {"en": u"Route reference in name tag", "fr": u"Référence d'une route dans le champ name"} }

        import re
        #self.ReRefRoute = re.compile(u"^[NDCEA] ?[0-9]+(| ?[a-z]| ?bis)$")
        self.ReRefRoute1 = re.compile(u".*[^RV]([RV]?([NDCEA] ?[0-9]+[^ ]*)).*")
        self.ReRefRoute2 = re.compile(u".*[nN][o°] ?[0-9]+[^ ]*")
        self.MultipleSpace = re.compile(u" +")

    def way(self, data, tags, nds):
        if "name" not in tags or "highway" not in tags or "ref" in tags:
            return

        ref = self.ReRefRoute1.match(tags["name"])
        if ref:
            ref_src = ref.group(1)
            ref_dest = ref.group(2)
            if " la %s" % ref_src in tags["name"] or " de %s" % ref_src in tags["name"] or " du %s" % ref_src in tags["name"]:
                return
            if "ancienne" in tags["name"]:
                return [(904, 0, {})]
            name = re.sub(self.MultipleSpace, " ", tags["name"].replace(ref_src, "").strip())
            if name == "":
                fix = {"-":["name"], "+":{"ref": ref_dest}}
            else:
                fix = {"~":{"name": name}, "+":{"ref": ref_dest}}
            return [(904, 0, {"fix": fix})]

        if self.ReRefRoute2.match(tags["name"]):
            return [(904, 1, {"en": "name=%s" % tags["name"]})]


if __name__ == "__main__":
    a = TagRemove_NameIsRef(None)
    a.init(None)
    name = {u"Route des Poules N10 vers le poulailler": u"Route des Poules vers le poulailler",
        u"Chemin de la C6 au moulin": u"Chemin de la C6 au moulin",
        u"Ancienne RN 7": u"Ancienne",
    }
    for n in name:
        rdp = a.way(None, {"name": n, "highway": "H"}, None)
        if rdp and rdp[0][2]["fix"]["~"]["name"] != name[n]:
            print "fail %s => %s" % (n, rdp[0][2]["fix"]["~"]["name"])